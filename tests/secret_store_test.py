import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET_STORE_PATH = ROOT / "package" / "contents" / "scripts" / "secret_store.py"


def load_secret_store():
    spec = importlib.util.spec_from_file_location("secret_store", SECRET_STORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.secret_store = load_secret_store()

    def test_read_command_does_not_write_secret_to_stdout(self):
        self.secret_store.lookup_secret = lambda attributes: (True, "stored-refresh-token")
        args = argparse.Namespace(scope="google-account", key="refresh_token", account_id="acc_1")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.secret_store.run_read(args)

        self.assertNotEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("stored-refresh-token", stderr.getvalue())

    def read_once_payload(self):
        server = self.secret_store.OnceServer(
            ("127.0.0.1", 0),
            self.secret_store.ReadOnceHandler,
            attributes=[("app", "test")],
            secret_token="test-token",
        )
        server.timeout = 2
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:{}/read".format(server.server_address[1]),
                headers={"Authorization": "Bearer test-token"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        finally:
            thread.join(timeout=2)
            server.server_close()

    def test_read_once_tells_not_found_from_errors(self):
        self.secret_store.lookup_secret_detailed = lambda attributes: (False, "", "")
        self.assertEqual(self.read_once_payload(), {"found": False, "value": ""})
        self.secret_store.lookup_secret_detailed = lambda attributes: (False, "", "No supported KWallet is available or unlocked.")
        self.assertEqual(self.read_once_payload(), {"found": False, "value": "", "error": "No supported KWallet is available or unlocked."})

    def test_lookup_detailed_combines_backends(self):
        store = self.secret_store

        def with_backends(secret_tool, kwallet):
            store.lookup_status_with_secret_tool = lambda attributes: secret_tool
            store.lookup_status_with_kwallet = lambda attributes: kwallet
            return store.lookup_secret_detailed([("app", "test")])

        self.assertEqual(with_backends(("found", "v", ""), ("error", "", "x")), (True, "v", ""))
        self.assertEqual(with_backends(("error", "", "dbus"), ("found", "w", "")), (True, "w", ""))
        self.assertEqual(with_backends(("missing", "", ""), ("error", "", "locked")), (False, "", ""))
        self.assertEqual(with_backends(("unavailable", "", ""), ("missing", "", "")), (False, "", ""))
        self.assertEqual(with_backends(("error", "", "dbus"), ("error", "", "locked")), (False, "", "dbus locked"))
        found, value, error = with_backends(("unavailable", "", ""), ("unavailable", "", ""))
        self.assertFalse(found)
        self.assertTrue(error)
        self.assertEqual(store.lookup_secret([("app", "test")]), (False, ""))

    def test_read_once_requires_authorization_and_returns_secret_as_json(self):
        self.secret_store.lookup_secret_detailed = lambda attributes: (True, "stored-refresh-token", "")
        server = self.secret_store.OnceServer(
            ("127.0.0.1", 0),
            self.secret_store.ReadOnceHandler,
            attributes=[("app", "test")],
            secret_token="test-token",
        )
        server.timeout = 2

        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:{}/read".format(server.server_address[1]),
                headers={"Authorization": "Bearer test-token"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(payload, {"found": True, "value": "stored-refresh-token"})
        self.assertTrue(server.shutdown_requested)
        self.assertEqual(server.result, 0)

    def test_store_once_serves_one_authorized_request(self):
        stored = []
        self.secret_store.store_secret = lambda attributes, label, value: (stored.append((attributes, label, value)) or (True, ""))
        ready_file = pathlib.Path(tempfile.mkdtemp()) / "ready.json"
        args = argparse.Namespace(scope="google-account", key="refresh_token", account_id="acc_1",
                                  ready_file=str(ready_file), timeout=5)
        results = []
        thread = threading.Thread(target=lambda: results.append(self.secret_store.run_store_once(args)))
        thread.start()
        try:
            for _ in range(50):
                if ready_file.exists():
                    break
                time.sleep(0.05)
            ready = json.loads(ready_file.read_text())
            url = "http://127.0.0.1:{}/store".format(ready["port"])
            body = json.dumps({"value": "new-refresh-token"}).encode("utf-8")

            wrong = urllib.request.Request(url, data=body, headers={"Authorization": "Bearer nope"})
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(wrong, timeout=2)
            self.assertEqual(denied.exception.code, 403)

            right = urllib.request.Request(url, data=body, headers={"Authorization": "Bearer " + ready["token"]})
            with urllib.request.urlopen(right, timeout=2) as response:
                self.assertEqual(json.loads(response.read().decode("utf-8")), {"ok": True})
        finally:
            thread.join(timeout=5)

        self.assertEqual(results, [0])
        self.assertFalse(ready_file.exists(), "the ready file is removed")
        self.assertEqual(stored, [(
            [("app", "plasma-applet-eventcalendar"), ("scope", "google-account"), ("key", "refresh_token"), ("account_id", "acc_1")],
            "plasma-applet-eventcalendar google-account refresh_token",
            "new-refresh-token",
        )])

    def test_store_secret_reports_missing_backends(self):
        self.secret_store.secret_tool_path = lambda: None
        self.secret_store.kwallet_query_path = lambda: None

        ok, error = self.secret_store.store_secret(
            [("app", "test")],
            "Test Secret",
            "stored-refresh-token",
        )

        self.assertFalse(ok)
        self.assertIn("secret-tool", error)
        self.assertIn("kwallet-query", error)

    def use_fake_kwallet(self, kwallet_query_writes=True):
        """KWallet behind kwallet-query and qdbus6, as on Plasma 6: kwallet-query -w only overwrites an
        existing entry and exits 0 without writing anything for a new one or a missing folder."""
        wallet = {}  # folder -> {entry: value}
        argvs = []
        store = self.secret_store

        class Proc:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

        def run(cmd, input=None, text=None, capture_output=None, timeout=None):
            argvs.append(list(cmd))
            if cmd[0] == "qdbus6":
                method, args = cmd[3].rsplit(".", 1)[1], cmd[4:]
                if method == "open":
                    return Proc(stdout="7\n")
                if method == "close":
                    return Proc(stdout="0\n")
                folder = args[1]
                if method == "hasFolder":
                    return Proc(stdout=("true" if folder in wallet else "false") + "\n")
                if method == "createFolder":
                    wallet.setdefault(folder, {})
                    return Proc(stdout="true\n")
                if method == "hasEntry":
                    return Proc(stdout=("true" if args[2] in wallet.get(folder, {}) else "false") + "\n")
                if method == "writePassword":
                    wallet[folder][args[2]] = args[3]
                    return Proc(stdout="0\n")
                if method == "removeEntry":
                    found = wallet.get(folder, {}).pop(args[2], None) is not None
                    return Proc(stdout=("0" if found else "-1") + "\n")
                raise AssertionError("unexpected D-Bus call " + method)
            self.assertEqual(cmd[0], "kwallet-query")
            if "-l" in cmd and "-f" not in cmd:
                return Proc()
            folder = cmd[cmd.index("-f") + 1]
            if "-w" in cmd:
                entry = cmd[cmd.index("-w") + 1]
                if kwallet_query_writes and entry in wallet.get(folder, {}):
                    wallet[folder][entry] = input
                return Proc()
            entry = cmd[cmd.index("-r") + 1]
            if entry not in wallet.get(folder, {}):
                return Proc(returncode=4, stdout="Failed to read entry\n")
            return Proc(stdout=wallet[folder][entry] + "\n")

        store.subprocess = argparse.Namespace(run=run, SubprocessError=Exception)
        store.shutil = argparse.Namespace(which=lambda name: name if name in ("kwallet-query", "qdbus6") else None)
        return wallet, argvs

    def test_kwallet_store_creates_new_entry_and_keeps_secret_off_argv(self):
        wallet, argvs = self.use_fake_kwallet()
        attributes = [("app", "plasma-applet-eventcalendar"), ("scope", "caldav-account"), ("key", "password")]

        self.assertEqual(self.secret_store.store_secret(attributes, "Test", "app-pass-1"), (True, ""))
        self.assertEqual(self.secret_store.lookup_secret(attributes), (True, "app-pass-1"))
        self.assertEqual(self.secret_store.store_secret(attributes, "Test", "app-pass-2"), (True, ""))
        self.assertEqual(self.secret_store.lookup_secret(attributes), (True, "app-pass-2"))
        self.assertFalse(any("app-pass" in arg for argv in argvs for arg in argv))

        self.assertTrue(self.secret_store.clear_secret(attributes))
        self.assertEqual(self.secret_store.lookup_secret_detailed(attributes), (False, "", ""))
        self.assertEqual(wallet["plasma-applet-eventcalendar"], {})

    def test_kwallet_store_reports_a_write_that_did_not_happen(self):
        wallet, _ = self.use_fake_kwallet(kwallet_query_writes=False)
        attributes = [("app", "plasma-applet-eventcalendar"), ("scope", "caldav-account"), ("key", "password")]

        ok, error = self.secret_store.store_secret(attributes, "Test", "app-pass")

        self.assertFalse(ok)
        self.assertIn("kwallet-query", error)
        self.assertEqual(wallet["plasma-applet-eventcalendar"], {})  # No placeholder left behind

    def test_kwallet_without_qdbus_still_overwrites_existing_entries(self):
        wallet, argvs = self.use_fake_kwallet()
        self.secret_store.shutil = argparse.Namespace(which=lambda name: name if name == "kwallet-query" else None)
        attributes = [("app", "plasma-applet-eventcalendar"), ("scope", "caldav-account"), ("key", "password")]
        entry = self.secret_store.kwallet_entry_name(attributes)

        ok, error = self.secret_store.store_secret(attributes, "Test", "app-pass")  # New: can't be made
        self.assertFalse(ok)
        self.assertIn("qdbus6", error)

        wallet["plasma-applet-eventcalendar"] = {entry: "old"}
        self.assertEqual(self.secret_store.store_secret(attributes, "Test", "app-pass"), (True, ""))
        self.assertEqual(wallet["plasma-applet-eventcalendar"][entry], "app-pass")
        self.assertTrue(self.secret_store.clear_secret(attributes))  # Emptied, as before
        self.assertEqual(wallet["plasma-applet-eventcalendar"][entry], "")
        self.assertFalse(any(argv[0] == "qdbus6" for argv in argvs))

    def test_scope_allowlist(self):
        parser = self.secret_store.build_parser()
        for scope in ("google-account", "ticktick-account", "caldav-account"):
            args = parser.parse_args(["clear", "--scope", scope, "--key", "token", "--account-id", "tt_1"])
            self.assertEqual(args.scope, scope)
            self.assertEqual(
                self.secret_store.build_attributes(args),
                [("app", "plasma-applet-eventcalendar"), ("scope", scope), ("key", "token"), ("account_id", "tt_1")],
            )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["clear", "--scope", "other", "--key", "token"])


if __name__ == "__main__":
    unittest.main()
