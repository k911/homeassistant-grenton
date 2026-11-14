"""Stable index comparisons and offline failure behavior without external I/O."""

import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import commands
from commands import create_parser
from firmware_catalog import MAX_INDEX_BYTES, check_firmware, compare_device, fetch_catalog, parse_catalog

# Representative unsorted entries from the vendor's captured firmware.list.
INDEX = """curl https://objectmanager.grenton.com/firmware/v5/firmware.list
CLU_ZWAVE_2-19-1-3-5.13.07-2436.fw
CLU_ZWAVE_2-19-1-3-5.04.09-1944B.fw
CLU_ZWAVE_2-19-1-3-5.15.24-2635.fw
CLU_ZWAVE_2-19-1-3-5.15.24-2635.json
CLU_ZWAVE_2-19-1-3-5.3.6-1927A.fw
CLU_GATE_HTTP-18-2-3-1.4.2-2346.fw
RELAY_DIN_4-21-1-2-2.0.0-2325.fw
firmware.list
firmware.list.prev
"""

# Versions and hardware signatures reported by a real CLU firmware inventory.
TFBUS_INDEX = INDEX + """TOUCH_PANEL_FM_8-27-2-2-1.2.7-2228.fw
ANALOG_IN_DIN-25-2-2-1.2.13-2125.fw
ROLLER_SH_DIN_3-42-1-2-3.2.9-2626.fw
IO_MODULE_DIN_8-30-1-2-2.0.2-2438A.fw
DIMMER_MOSFET_DIN-26-1-2-1.1.13-2308A.fw
DIGITAL_IN_FM-51-1-2-1.1.4-2334.fw
LED_RGBW_FM-29-1-2-3.0.1-2629.fw
LED_RGBW_DIN-24-1-2-4.0.3-2629.fw
"""
TFBUS_DEVICES = [
    (27, 2, 2, "1.2.7", "version_matches"),
    (25, 2, 2, "1.2.13", "version_matches"),
    (42, 1, 2, "3.2.9", "version_matches"),
    (30, 1, 2, "2.0.2", "version_matches"),
    (21, 1, 2, "2.0.0", "version_matches"),
    (26, 1, 2, "2.1.2", "newer_than_stable"),
    (51, 1, 2, "1.1.4", "version_matches"),
    (3, 1, 3, "6.3.7", "unknown"),
    (29, 1, 2, "3.0.1", "version_matches"),
    (24, 1, 2, "4.0.3", "version_matches"),
]


def tfbus_devices():
    return [
        {"sn": index, "hwType": hw_type, "hwVer": hw_ver,
         "fwType": fw_type, "fwVer": version}
        for index, (hw_type, hw_ver, fw_type, version, _) in enumerate(TFBUS_DEVICES)
    ]


def report(version="5.15.24-2635"):
    return {"sn": 221000001, "hwType": 19, "hwVer": 1, "fwType": 3, "fwVer": version,
            "tfbusDiagnostic": [{"sn": 1, "hwType": 21, "hwVer": 1, "fwType": 2, "fwVer": "1.3.13-2125"}],
            "zwaveDiagnostic": []}


class CatalogTests(unittest.TestCase):
    def test_unsorted_index_uses_numeric_versions_and_ignores_non_firmware_lines(self):
        catalog = parse_catalog(INDEX.replace("\n", "\r\n"))
        self.assertEqual(catalog[(19, 1, 3)], ("5.15.24-2635", "CLU_ZWAVE_2-19-1-3-5.15.24-2635.fw"))
        self.assertEqual(len(catalog), 3)

    def test_full_hardware_signature_is_required(self):
        catalog = parse_catalog(INDEX)
        for field in ("hwType", "hwVer", "fwType"):
            self.assertEqual(compare_device({**report(), field: 999}, catalog)["status"], "unknown")
        for field in ("hwType", "hwVer", "fwType", "fwVer"):
            invalid = report()
            invalid.pop(field)
            self.assertEqual(compare_device(invalid, catalog)["status"], "unknown")

    def test_current_outdated_and_beta_newer_than_stable_are_distinct(self):
        catalog = parse_catalog(INDEX)
        for version, status in (("5.15.24-2635", "up_to_date"), ("5.9.02-2208", "update_available"),
                                ("5.16.03-2644", "newer_than_stable")):
            self.assertEqual(compare_device(report(version), catalog)["status"], status)

    def test_build_letter_revisions_are_ordered_and_manifest_only_is_not_a_release(self):
        catalog = parse_catalog("CLU_GATE_HTTP-18-2-3-1.1.0-2034C.fw\nCLU_GATE_HTTP-18-2-3-1.1.0-2034.fw\n")
        self.assertEqual(catalog[(18, 2, 3)][0], "1.1.0-2034C")
        for contents in ("<html>blocked</html>", "CLU_ZWAVE_2-19-1-3-5.15.24-2635.json"):
            with self.assertRaises(ValueError):
                parse_catalog(contents)

    def test_real_tfbus_inventory_compares_versions_without_builds(self):
        catalog = parse_catalog(TFBUS_INDEX)
        for device, (_, _, _, _, expected) in zip(tfbus_devices(), TFBUS_DEVICES):
            with self.subTest(device=device):
                result = compare_device(device, catalog)
                self.assertEqual(result["status"], expected)
                if expected == "unknown":
                    self.assertIn("No matching hardware", result["reason"])
                else:
                    self.assertEqual(result["comparison_scope"], "version")
                    self.assertNotEqual(result["status"], "up_to_date")

    def test_release_only_versions_order_numerically_without_inventing_a_build(self):
        catalog = parse_catalog(TFBUS_INDEX)
        for version, expected in (("1.2.6", "update_available"),
                                  ("1.02.007", "version_matches"),
                                  ("1.2.8", "newer_than_stable"),
                                  ("1.10.1", "newer_than_stable")):
            device = {"hwType": 27, "hwVer": 2, "fwType": 2, "fwVer": version}
            with self.subTest(version=version):
                result = compare_device(device, catalog)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["comparison_scope"], "version")

    def test_reported_builds_still_compare_exact_build_and_letter_revision(self):
        catalog = parse_catalog(TFBUS_INDEX)
        for version, expected in (("2.0.2-2438", "update_available"),
                                  ("2.0.2-2438A", "up_to_date"),
                                  ("2.0.2-2438B", "newer_than_stable")):
            result = compare_device({"hwType": 30, "hwVer": 1, "fwType": 2, "fwVer": version}, catalog)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["comparison_scope"], "version_and_build")

    def test_malformed_partial_versions_stay_unknown(self):
        catalog = parse_catalog(TFBUS_INDEX)
        for version in (None, 1.2, "1.2", "1.2.7-", "1.2.7-222", "1.2.7junk", "v1.2.7"):
            result = compare_device({"hwType": 27, "hwVer": 2, "fwType": 2, "fwVer": version}, catalog)
            self.assertEqual(result["status"], "unknown")

    def test_both_cli_reports_show_release_only_comparisons_in_text_and_json(self):
        for kind in ("diagnostics", "firmware"):
            for output_format in ("text", "json"):
                with self.subTest(kind=kind, output=output_format):
                    inventory = report()
                    inventory.pop("tfbusDiagnostic")
                    inventory.pop("zwaveDiagnostic")
                    tfbus_key = "tfbusDiagnostic" if kind == "diagnostics" else "tfbusDevices"
                    zwave_key = "zwaveDiagnostic" if kind == "diagnostics" else "zwaveDevices"
                    inventory.update({tfbus_key: tfbus_devices(), zwave_key: []})
                    manager = SimpleNamespace(**{kind: AsyncMock(return_value=inventory)})
                    args = create_parser().parse_args([kind, "--output", output_format])
                    output = io.StringIO()
                    with (patch("commands.diagnostics.GrentonManager", return_value=manager),
                          patch("firmware_catalog.fetch_catalog", AsyncMock(return_value=parse_catalog(TFBUS_INDEX))),
                          contextlib.redirect_stdout(output)):
                        args.handler(args)
                    if output_format == "json":
                        result = json.loads(output.getvalue())["firmware_check"]
                        self.assertEqual(result["status"], "up_to_date")
                        self.assertEqual([device["status"] for device in result["devices"]],
                                         [device[-1] for device in TFBUS_DEVICES])
                    else:
                        self.assertIn("version matches; installed 1.2.7, latest stable 1.2.7-2228; installed build not reported", output.getvalue())
                        self.assertNotIn("Unrecognized installed firmware version", output.getvalue())

    def test_cli_enriches_json_by_default_and_keeps_local_fields(self):
        args = create_parser().parse_args(["diagnostics", "--output", "json"])
        manager = SimpleNamespace(diagnostics=AsyncMock(return_value=report()))
        output = io.StringIO()
        with (patch("commands.diagnostics.GrentonManager", return_value=manager),
              patch("firmware_catalog.fetch_catalog", AsyncMock(return_value=parse_catalog(INDEX))),
              contextlib.redirect_stdout(output)):
            args.handler(args)
        result = json.loads(output.getvalue())
        self.assertEqual(result["fwVer"], report()["fwVer"])
        self.assertEqual(result["firmware_check"]["status"], "up_to_date")
        self.assertEqual(result["firmware_check"]["devices"][0]["status"], "update_available")


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_failure_preserves_diagnostics_and_is_explicit(self):
        for error in (TimeoutError(), ValueError("HTTP 403")):
            with patch("firmware_catalog.fetch_catalog", AsyncMock(side_effect=error)):
                result = await check_firmware(report(), "diagnostics", timeout=1)
            self.assertEqual(result["status"], "unavailable")
            self.assertTrue(result["reason"])

    async def test_fetch_is_bounded_and_rejects_non_200_responses(self):
        async def chunks(data):
            yield data

        for status, data, message in ((403, b"", "HTTP 403"), (200, b"x" * (MAX_INDEX_BYTES + 1), "64 KiB")):
            response = SimpleNamespace(status=status, content=SimpleNamespace(iter_chunked=lambda _: chunks(data)))
            response_context = AsyncMock()
            response_context.__aenter__.return_value = response
            session = SimpleNamespace(get=lambda *args, **kwargs: response_context)
            session_context = AsyncMock()
            session_context.__aenter__.return_value = session
            with patch("firmware_catalog.aiohttp.ClientSession", return_value=session_context):
                with self.assertRaisesRegex(ValueError, message):
                    await fetch_catalog(1)


if __name__ == "__main__":
    unittest.main()
