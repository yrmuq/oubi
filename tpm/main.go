package main

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/google/go-tpm/tpm2/transport"
	"github.com/google/go-tpm/tpm2/transport/linuxtpm"
)

const (
	socketPath = "/run/oubi-tpm/service.sock"
	tpmDevice  = "/dev/tpmrm0"
	hostKey    = "/var/lib/oubi-tpm/host-key"
	maxMessage = 32768
)

type request struct {
	Operation string `json:"operation"`
	ID        string `json:"id"`
	Value     string `json:"value"`
}

type reply struct {
	Value string `json:"value,omitempty"`
	Error string `json:"error,omitempty"`
}

func validID(id string) bool {
	_, err := hex.DecodeString(id)
	return len(id) == 32 && err == nil && strings.ToLower(id) == id
}

func (store credentialStore) handle(req request) reply {
	switch req.Operation {
	case "ping":
		t, err := store.openTPM()
		if err != nil {
			return reply{Error: "unavailable"}
		}
		t.Close()
		return reply{Value: "ready"}
	case "encrypt", "decrypt":
		if !validID(req.ID) || req.Value == "" {
			return reply{Error: "invalid"}
		}
	default:
		return reply{Error: "invalid"}
	}

	var value string
	var err error
	if req.Operation == "encrypt" {
		if len(req.Value) > 4096 || strings.ContainsAny(req.Value, "\x00\r\n") {
			return reply{Error: "invalid"}
		}
		value, err = store.encrypt(req.ID, req.Value)
	} else {
		value, err = store.decrypt(req.ID, req.Value)
	}
	if errors.Is(err, errTPMUnavailable) {
		return reply{Error: "unavailable"}
	}
	if err != nil {
		return reply{Error: "credential"}
	}
	return reply{Value: value}
}

func authorized(conn *net.UnixConn, uid uint32) bool {
	raw, err := conn.SyscallConn()
	if err != nil {
		return false
	}
	var credentials *syscall.Ucred
	var optionsErr error
	err = raw.Control(func(fd uintptr) {
		credentials, optionsErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	})
	return err == nil && optionsErr == nil && credentials != nil && credentials.Uid == uid
}

func serve(conn *net.UnixConn, uid uint32, store credentialStore) {
	defer conn.Close()
	if !authorized(conn, uid) || conn.SetDeadline(time.Now().Add(30*time.Second)) != nil {
		return
	}
	// A request ends when the client closes its write half of the connection.
	data, err := io.ReadAll(io.LimitReader(conn, maxMessage+1))
	var req request
	response := reply{Error: "invalid"}
	if err == nil && len(data) <= maxMessage && json.Unmarshal(data, &req) == nil {
		response = store.handle(req)
	}
	clear(data)
	json.NewEncoder(conn).Encode(response)
}

func listen(path string) (*net.UnixListener, error) {
	info, err := os.Lstat(path)
	if err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return nil, fmt.Errorf("socket path is occupied by a non-socket")
		}
		conn, err := net.DialTimeout("unix", path, time.Second)
		if err == nil {
			conn.Close()
			return nil, fmt.Errorf("TPM service is already listening")
		}
		if !errors.Is(err, syscall.ECONNREFUSED) {
			return nil, err
		}
		if err := os.Remove(path); err != nil {
			return nil, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return nil, err
	}
	// The socket volume is mounted only in Oubi. The kernel supplies the client's UID.
	if err := os.Chmod(path, 0666); err != nil {
		listener.Close()
		return nil, err
	}
	return listener, nil
}

func run() error {
	uidValue := os.Getenv("OUBI_TPM_UID")
	if uidValue == "" {
		uidValue = "1000"
	}
	uid, err := strconv.ParseUint(uidValue, 10, 32)
	if err != nil {
		return fmt.Errorf("OUBI_TPM_UID must be the numeric UID of Oubi")
	}
	data, err := os.ReadFile("/etc/machine-id")
	if err != nil {
		return fmt.Errorf("read host machine ID: %w", err)
	}
	machineID := strings.TrimSpace(string(data))
	if !validID(machineID) || machineID == strings.Repeat("0", 32) {
		return fmt.Errorf("host /etc/machine-id must contain a valid machine ID")
	}
	store := credentialStore{
		machineID: machineID,
		keyPath:   hostKey,
		openTPM: func() (transport.TPMCloser, error) {
			return linuxtpm.Open(tpmDevice)
		},
	}
	listener, err := listen(socketPath)
	if err != nil {
		return fmt.Errorf("listen on TPM socket: %w", err)
	}
	defer listener.Close()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	go func() {
		<-signals
		listener.Close()
	}()
	for {
		conn, err := listener.AcceptUnix()
		if errors.Is(err, net.ErrClosed) {
			return nil
		}
		if err != nil {
			return err
		}
		serve(conn, uint32(uid), store)
	}
}

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
