"""The network-device-identifiers policy (a-062, build 2026-09-26): real client
MACs, controller address, prompt host and SNMP user in a recovered walk are
pseudonymised deterministically, consistently in every form, and replayably."""
import json
import unittest

from pipeline.ledger.redaction import KINDS, transform

KIND = ["network-device-identifiers"]
WALK = ("@srv-zbx-dev-10:/data/zabbix/scripts/snmp$ sudo python3 report.py\n"
        "12:26:28 INFO Poll 10.10.10.1 (SNMPv3 user=zbxmon level=authPriv)\n"
        "iso.3.6.1.4.1.9.1.7.1.1.1.0.23.200.210.96.77 = Hex-STRING: 00 17 C8 D2 60 4D\n"
        "iso.3.6.1.4.1.9.1.7.1.1.1.2.34.43.145.188.28 = Hex-STRING: 02 22 2B 91 BC 1C\n")


class NetworkIdentifiers(unittest.TestCase):
    def test_every_identifier_is_replaced_and_forms_stay_paired(self):
        self.assertIn("network-device-identifiers", KINDS)
        out, changes = transform(WALK, KIND)
        for real in ("srv-zbx-dev-10", "/data/zabbix", "10.10.10.1", "zbxmon",
                     "00 17 C8 D2 60 4D", "0.23.200.210.96.77", "02 22 2B 91 BC 1C"):
            self.assertNotIn(real, out)
        self.assertIn("@host:~/work$ sudo", out)
        self.assertIn("user=snmp-user", out)
        self.assertRegex(out, r"Poll 192\.0\.2\.\d+ ")
        for line in out.splitlines()[2:]:
            oid, hexes = line.split(" = Hex-STRING: ")
            tail = [int(x) for x in oid.split(".")[-6:]]
            self.assertEqual(tail, [int(x, 16) for x in hexes.split()])   # still the same device
            self.assertTrue(hexes.startswith("00 00 5E 00 53 "))           # RFC 7042 documentation block
        self.assertEqual(len(changes), 7)

    def test_one_device_gets_one_pseudonym_in_every_file_and_form(self):
        walk, _ = transform(WALK, KIND)
        note, _ = transform("index from 6 numbers (.0.23.200.210.96.77) [vendor]", KIND)
        paired = walk.splitlines()[2].split(" = ")[0].split(".")[-6:]
        self.assertIn("(." + ".".join(paired) + ")", note)

    def test_bare_hex_and_octet_list_spellings_are_caught(self):
        # a-062's poller kept the real MAC as '"00 17 C8 D2 60 4D"' and '[0,23,200,210,96,77]'.
        walk, _ = transform(WALK, KIND)
        pseudo = [int(x) for x in walk.splitlines()[2].split(" = ")[0].split(".")[-6:]]
        code, _ = transform('# "00 17 C8 D2 60 4D" -> token\nints = [0,23,200,210,96,77]\n', KIND)
        self.assertNotIn("00 17 C8 D2 60 4D", code)
        self.assertIn(" ".join(f"{b:02X}" for b in pseudo), code)
        self.assertIn("[" + ",".join(map(str, pseudo)) + "]", code)

    def test_replay_is_exact_and_json_records_stay_valid(self):
        record = json.dumps({"kind": "user_text", "text": WALK}, ensure_ascii=False)
        once, _ = transform(record, KIND)
        self.assertEqual(once, transform(record, KIND)[0])
        self.assertNotIn("zbxmon", json.loads(once)["text"])

    def test_placeholders_public_addresses_and_code_are_left_alone(self):
        text = ('broadcast FF:FF:FF:FF:FF:FF example AA-BB-CC-DD-EE-FF zero 00 00 00 00 00 00\n'
                'dns 8.8.8.8\nlog.info("Poll %s (SNMPv3 user=%s level=%s)", host, user, lvl)\n'
                'doc 00:00:5E:00:53:10\n')
        with self.assertRaisesRegex(ValueError, "no supported private data"):
            transform(text, KIND)


if __name__ == "__main__":
    unittest.main()
