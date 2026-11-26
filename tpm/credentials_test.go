package main

import (
	"bytes"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/go-tpm/tpm2"
	"github.com/google/go-tpm/tpm2/transport"
)

func TestHostKey(t *testing.T) {
	path := filepath.Join(t.TempDir(), "key")
	if _, err := readKey(path, false); !os.IsNotExist(err) {
		t.Fatalf("unexpected result for a missing key: %v", err)
	}
	key, err := readKey(path, true)
	if err != nil || len(key) != 32 {
		t.Fatalf("create key: %v", err)
	}
	again, err := readKey(path, true)
	if err != nil || !bytes.Equal(key, again) {
		t.Fatalf("the host key must persist: %v", err)
	}
	link := filepath.Join(filepath.Dir(path), "link")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readKey(link, true); err == nil {
		t.Fatal("accepted a symlink as a host key")
	}
}

func TestCredentialRoundTripWithTPM(t *testing.T) {
	address := os.Getenv("OUBI_TEST_TPM")
	if address == "" {
		t.Skip("set OUBI_TEST_TPM to a disposable swtpm TCP server to exercise the TPM")
	}
	open := func() (transport.TPMCloser, error) {
		conn, err := net.DialTimeout("tcp", address, 2*time.Second)
		if err != nil {
			return nil, err
		}
		conn.SetDeadline(time.Now().Add(5 * time.Second))
		return transport.FromReadWriteCloser(conn), nil
	}
	store := credentialStore{
		machineID: strings.Repeat("1", 32),
		keyPath:   filepath.Join(t.TempDir(), "key"),
		openTPM:   open,
	}
	id := strings.Repeat("a", 32)
	ciphertext := store.handle(request{Operation: "encrypt", ID: id, Value: "test password"})
	if !strings.HasPrefix(ciphertext.Value, credentialPrefix) {
		t.Fatalf("seal credential: %v", ciphertext.Error)
	}
	req := request{Operation: "decrypt", ID: id, Value: ciphertext.Value}
	if got := store.handle(req); got.Value != "test password" {
		t.Fatalf("unseal credential: %v", got.Error)
	}

	otherID := req
	otherID.ID = strings.Repeat("b", 32)
	if got := store.handle(otherID); got.Error != "credential" {
		t.Fatal("accepted a credential for another schedule")
	}
	otherMachine := store
	otherMachine.machineID = strings.Repeat("2", 32)
	if got := otherMachine.handle(req); got.Error != "credential" {
		t.Fatal("accepted a credential for another machine ID")
	}
	otherHost := store
	otherHost.keyPath = filepath.Join(t.TempDir(), "key")
	if _, err := readKey(otherHost.keyPath, true); err != nil {
		t.Fatal(err)
	}
	if got := otherHost.handle(req); got.Error != "credential" {
		t.Fatal("accepted a credential with a different host key")
	}

	conn, err := open()
	if err != nil {
		t.Fatal(err)
	}
	_, err = (&tpm2.PCRExtend{
		PCRHandle: tpm2.AuthHandle{Handle: 7, Auth: tpm2.PasswordAuth(nil)},
		Digests: tpm2.TPMLDigestValues{Digests: []tpm2.TPMTHA{
			{HashAlg: tpm2.TPMAlgSHA256, Digest: bytes.Repeat([]byte{0x42}, 32)},
		}},
	}).Execute(conn)
	conn.Close()
	if err != nil {
		t.Fatal(err)
	}
	if got := store.handle(req); got.Error != "credential" {
		t.Fatal("accepted a credential with a changed PCR 7")
	}
}
