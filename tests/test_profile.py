from base64 import b64decode
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from buzz_server.identity import public, schnorr_sign
from buzz_server.profile import post, publish_profile
from fixture_relay import Relay


KEY = "0" * 63 + "3"


class ProfileTests(unittest.TestCase):
    def test_schnorr_signing_matches_bip340_vectors(self):
        # https://github.com/bitcoin/bips/blob/master/bip-0340/test-vectors.csv
        vectors = [
            (KEY, "00" * 32, "00" * 32,
             "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525"
             "F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"),
            ("C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
             "C87AA53824B4D7AE2EB035A2B5BBBCCC080E76CDC6D1692C4B0B62D798E6D906",
             "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
             "5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1BA"
             "B745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7"),
        ]
        for key, auxiliary, message, expected in vectors:
            with self.subTest(public_key=public(key)), patch("buzz_server.identity.secrets.token_bytes", return_value=bytes.fromhex(auxiliary)):
                self.assertEqual(schnorr_sign(key, bytes.fromhex(message)).hex(), expected.lower())

    def test_first_admitted_publication_and_later_edits_are_agent_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(root)
            serving = Thread(target=relay.serve_forever, daemon=True)
            serving.start()
            try:
                (root / "restricted").touch()
                relay_url = f"ws://127.0.0.1:{relay.server_port}/"
                pubkey = public(KEY)
                profile = {"name": 'Synthetic "agent" ☕', "about": "one\ntwo", "avatar": "https://example.test/avatar"}
                profile_path = root / "profile.json"
                refused = publish_profile(relay_url, profile, KEY, pubkey)
                self.assertIs(refused["accepted"], False)
                self.assertTrue(refused["message"].startswith("restricted: "))
                self.assertFalse(profile_path.exists())
                (root / "admitted-pubkey").write_text(pubkey)

                previous = 0
                for name, about, avatar in [(profile["name"], profile["about"], profile["avatar"]),
                                            ("Edited agent", "", ""), ("Edited agent", "", "")]:
                    with self.subTest(name=name, previous=previous):
                        edited = {"name": name, "about": about, "avatar": avatar}
                        self.assertIs(publish_profile(relay_url, edited, KEY, pubkey)["accepted"], True)
                        event = json.loads(profile_path.read_text())
                        self.assertEqual((event["kind"], event["pubkey"], event["tags"]), (0, pubkey, []))
                        self.assertEqual(json.loads(event["content"]), {
                            "display_name": name, "about": about, "picture": avatar, "is_agent": True})
                        canonical = [0, pubkey, event["created_at"], 0, [], event["content"]]
                        raw = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False).encode()
                        self.assertEqual(event["id"], sha256(raw).hexdigest())
                        self.assertRegex(event["sig"], r"^[0-9a-f]{128}$")
                        self.assertGreater(event["created_at"], previous)
                        previous = event["created_at"]
            finally:
                relay.shutdown()
                relay.server_close()

    def test_http_proof_binds_destination_method_and_exact_body(self):
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        response.read.return_value = b"[]"
        pubkey = public(KEY)
        with patch("buzz_server.profile.HTTPSConnection") as connect:
            connection = connect.return_value
            connection.getresponse.return_value = response
            relay = urlsplit("wss://Relay.Example.test:8443/nested/")
            for _ in range(2):
                self.assertEqual(post(relay, "/query", [{"kinds": [0]}], KEY, pubkey), [])

        nonces = set()
        for request in connection.request.call_args_list:
            method, path, body, headers = request.args
            self.assertEqual((method, path), ("POST", "/nested/query"))
            proof = json.loads(b64decode(headers["Authorization"].removeprefix("Nostr ")))
            tags = dict(proof["tags"])
            self.assertEqual((proof["kind"], proof["pubkey"], proof["content"]), (27235, pubkey, ""))
            self.assertEqual(tags.pop("u"), "https://relay.example.test:8443/nested/query")
            self.assertEqual(tags.pop("method"), "POST")
            self.assertEqual(tags.pop("payload"), sha256(body).hexdigest())
            nonces.add(tags.pop("nonce"))
            self.assertEqual(tags, {})
        self.assertEqual(len(nonces), 2)
