package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/google/go-tpm/tpm2"
	"github.com/google/go-tpm/tpm2/transport"
)

const credentialPrefix = "oubi-tpm-v1:"

var (
	errTPMUnavailable = errors.New("TPM device unavailable")
	pcrSelection      = tpm2.TPMLPCRSelection{PCRSelections: []tpm2.TPMSPCRSelection{
		{Hash: tpm2.TPMAlgSHA256, PCRSelect: tpm2.PCClientCompatible.PCRs(7)},
	}}
)

type credentialStore struct {
	machineID string
	keyPath   string
	openTPM   func() (transport.TPMCloser, error)
}

type credential struct {
	Public  []byte `json:"public"`
	Private []byte `json:"private"`
	Nonce   []byte `json:"nonce"`
	Payload []byte `json:"payload"`
}

func createKey(path string) error {
	keyFile, err := os.CreateTemp(filepath.Dir(path), ".host-key-*")
	if err != nil {
		return err
	}
	defer os.Remove(keyFile.Name())
	defer keyFile.Close()

	key := make([]byte, 32)
	defer clear(key)
	if _, err := rand.Read(key); err != nil {
		return err
	}
	if _, err := keyFile.Write(key); err != nil {
		return err
	}
	if err := keyFile.Chmod(0400); err != nil {
		return err
	}
	if err := keyFile.Sync(); err != nil {
		return err
	}
	// Publish a complete key and retain the existing one on a concurrent startup.
	return os.Link(keyFile.Name(), path)
}

func readKey(path string, create bool) ([]byte, error) {
	if create {
		_, err := os.Lstat(path)
		if errors.Is(err, os.ErrNotExist) {
			if err := createKey(path); err != nil && !errors.Is(err, os.ErrExist) {
				return nil, err
			}
		}
	}
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0400 || info.Size() != 32 {
		return nil, fmt.Errorf("host key file has invalid type, permissions or size")
	}
	key := make([]byte, 32)
	if _, err := io.ReadFull(file, key); err != nil {
		clear(key)
		return nil, err
	}
	return key, nil
}

func (store credentialStore) cipher(host, sealed []byte, id string) (cipher.AEAD, error) {
	key := hmac.New(sha256.New, host)
	key.Write([]byte(credentialPrefix + store.machineID + id))
	key.Write(sealed)
	derived := key.Sum(nil)
	defer clear(derived)
	block, err := aes.NewCipher(derived)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func (store credentialStore) encrypt(id, password string) (string, error) {
	host, err := readKey(store.keyPath, true)
	if err != nil {
		return "", err
	}
	defer clear(host)
	t, primary, err := store.storageKey()
	if err != nil {
		return "", err
	}
	defer t.Close()
	defer (&tpm2.FlushContext{FlushHandle: primary.ObjectHandle}).Execute(t)

	public, err := primary.OutPublic.Contents()
	if err != nil {
		return "", err
	}
	policy, closePolicy, err := tpm2.PolicySession(t, tpm2.TPMAlgSHA256, 16)
	if err != nil {
		return "", err
	}
	defer closePolicy()
	if _, err := (&tpm2.PolicyPCR{PolicySession: policy.Handle(), Pcrs: pcrSelection}).Execute(t); err != nil {
		return "", err
	}
	digest, err := (&tpm2.PolicyGetDigest{PolicySession: policy.Handle()}).Execute(t)
	if err != nil {
		return "", err
	}

	secret := make([]byte, 32)
	defer clear(secret)
	if _, err := rand.Read(secret); err != nil {
		return "", err
	}
	salt := tpm2.Salted(primary.ObjectHandle, *public)
	sealed, err := (&tpm2.Create{
		ParentHandle: tpm2.AuthHandle{
			Handle: primary.ObjectHandle, Name: primary.Name,
			Auth: tpm2.HMAC(tpm2.TPMAlgSHA256, 16, salt, tpm2.AESEncryption(128, tpm2.EncryptIn)),
		},
		InSensitive: tpm2.TPM2BSensitiveCreate{Sensitive: &tpm2.TPMSSensitiveCreate{
			Data: tpm2.NewTPMUSensitiveCreate(&tpm2.TPM2BSensitiveData{Buffer: secret}),
		}},
		InPublic: tpm2.New2B(tpm2.TPMTPublic{
			Type: tpm2.TPMAlgKeyedHash, NameAlg: tpm2.TPMAlgSHA256, AuthPolicy: digest.PolicyDigest,
			ObjectAttributes: tpm2.TPMAObject{FixedTPM: true, FixedParent: true, NoDA: true},
		}),
	}).Execute(t)
	if err != nil {
		return "", err
	}
	gcm, err := store.cipher(host, secret, id)
	if err != nil {
		return "", err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	data, err := json.Marshal(credential{
		Public: tpm2.Marshal(sealed.OutPublic), Private: tpm2.Marshal(sealed.OutPrivate),
		Nonce: nonce, Payload: gcm.Seal(nil, nonce, []byte(password), []byte(id)),
	})
	if err != nil {
		return "", err
	}
	return credentialPrefix + base64.RawStdEncoding.EncodeToString(data), nil
}

func (store credentialStore) decrypt(id, encoded string) (string, error) {
	encoded, ok := strings.CutPrefix(encoded, credentialPrefix)
	if !ok {
		return "", fmt.Errorf("unsupported credential format")
	}
	data, err := base64.RawStdEncoding.DecodeString(encoded)
	if err != nil {
		return "", err
	}
	var sealed credential
	if err := json.Unmarshal(data, &sealed); err != nil {
		return "", err
	}
	public, err := tpm2.Unmarshal[tpm2.TPM2BPublic](sealed.Public)
	if err != nil {
		return "", err
	}
	private, err := tpm2.Unmarshal[tpm2.TPM2BPrivate](sealed.Private)
	if err != nil {
		return "", err
	}

	host, err := readKey(store.keyPath, false)
	if err != nil {
		return "", err
	}
	defer clear(host)
	t, primary, err := store.storageKey()
	if err != nil {
		return "", err
	}
	defer t.Close()
	defer (&tpm2.FlushContext{FlushHandle: primary.ObjectHandle}).Execute(t)
	parentPublic, err := primary.OutPublic.Contents()
	if err != nil {
		return "", err
	}
	loaded, err := (&tpm2.Load{
		ParentHandle: tpm2.AuthHandle{Handle: primary.ObjectHandle, Name: primary.Name, Auth: tpm2.PasswordAuth(nil)},
		InPublic:     *public, InPrivate: *private,
	}).Execute(t)
	if err != nil {
		return "", err
	}
	defer (&tpm2.FlushContext{FlushHandle: loaded.ObjectHandle}).Execute(t)

	salt := tpm2.Salted(primary.ObjectHandle, *parentPublic)
	policy, closePolicy, err := tpm2.PolicySession(t, tpm2.TPMAlgSHA256, 16, salt,
		tpm2.AESEncryption(128, tpm2.EncryptOut))
	if err != nil {
		return "", err
	}
	defer closePolicy()
	if _, err := (&tpm2.PolicyPCR{PolicySession: policy.Handle(), Pcrs: pcrSelection}).Execute(t); err != nil {
		return "", err
	}
	unsealed, err := (&tpm2.Unseal{
		ItemHandle: tpm2.AuthHandle{Handle: loaded.ObjectHandle, Name: loaded.Name, Auth: policy},
	}).Execute(t)
	if err != nil {
		return "", err
	}
	defer clear(unsealed.OutData.Buffer)
	gcm, err := store.cipher(host, unsealed.OutData.Buffer, id)
	if err != nil {
		return "", err
	}
	if len(sealed.Nonce) != gcm.NonceSize() {
		return "", fmt.Errorf("invalid credential nonce")
	}
	plaintext, err := gcm.Open(nil, sealed.Nonce, sealed.Payload, []byte(id))
	defer clear(plaintext)
	return string(plaintext), err
}

func (store credentialStore) storageKey() (transport.TPMCloser, *tpm2.CreatePrimaryResponse, error) {
	t, err := store.openTPM()
	if err != nil {
		return nil, nil, fmt.Errorf("%w: %v", errTPMUnavailable, err)
	}
	primary, err := (&tpm2.CreatePrimary{
		PrimaryHandle: tpm2.TPMRHOwner, InPublic: tpm2.New2B(tpm2.ECCSRKTemplate),
	}).Execute(t)
	if err != nil {
		t.Close()
		return nil, nil, err
	}
	return t, primary, nil
}
