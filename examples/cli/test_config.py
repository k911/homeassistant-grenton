"""Tests for configuration merging and tracked-object discovery."""
import tempfile
import unittest
from pathlib import Path

import yaml

from config import save_configuration


def _interface() -> dict:
    return {
        "id": "interface-new",
        "name": "Updated interface",
        "version": 2,
        "encryption": {"key": "new-key", "iv": "new-iv"},
        "clus": [
            {
                "id": "CLU1",
                "name": "Main CLU",
                "serialNumber": "123",
                "ip": "192.0.2.10",
                "port": 1234,
                "connectionType": "ETHERNET",
            }
        ],
        "pages": [
            {
                "widgets": [
                    {
                        "label": "First label",
                        "object": {
                            "value": {
                                "callType": "VARIABLE",
                                "cluId": "CLU1",
                                "objectName": "CLU1",
                                "index": "NewVariable",
                            },
                            "state": {
                                "callType": "ATTRIBUTE",
                                "cluId": "CLU1",
                                "objectName": "DIN1",
                                "index": 0,
                            },
                            "alsoState": {
                                "callType": "ATTRIBUTE",
                                "cluId": "CLU1",
                                "objectName": "DIN2",
                                "index": "1",
                            },
                        },
                    },
                    {
                        "label": "Later duplicate label",
                        "text": {
                            "callType": "VARIABLE",
                            "cluId": "CLU1",
                            "objectName": "CLU1",
                            "index": "NewVariable",
                        },
                        "components": [
                            {
                                "label": "Component label",
                                "state": {
                                    "callType": "VARIABLE",
                                    "cluId": "CLU1",
                                    "objectName": "CLU1",
                                    "index": "ComponentVariable",
                                },
                                "actions": [
                                    {
                                        "callType": "ATTRIBUTE",
                                        "cluId": "CLU1",
                                        "objectName": "DIN1",
                                        "index": 0,
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        ],
    }


def _write_existing(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "id": "interface-old",
                "custom_root": {"keep": True},
                "encryption": {"key": "old", "iv": "old", "custom": "keep"},
                "clus": [
                    {
                        "id": "CLU1",
                        "name": "Old name",
                        "ip": "old-ip",
                        "custom": "keep",
                    },
                    {"id": "CUSTOM", "name": "Manual CLU", "custom": True},
                ],
                "grenton_tracked_objects": {
                    "Main CLU": {
                        "custom_section": "keep",
                        "variables": {
                            "My custom variable group": [
                                {
                                    "name": "NewVariable",
                                    "description": "keep this",
                                    "sync": "slow",
                                }
                            ]
                        },
                        "attributes": {
                            "My custom attribute group": [
                                {
                                    "object": "DIN1",
                                    "index": "0",
                                    "description": "keep this too",
                                }
                            ]
                        },
                    }
                },
            },
            sort_keys=False,
        )
    )


class SaveConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "config.yaml"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_preserves_custom_configuration_and_tracked_objects_by_default(self):
        _write_existing(self.path)
        before = yaml.safe_load(self.path.read_text())["grenton_tracked_objects"]

        save_configuration(_interface(), self.path)

        saved = yaml.safe_load(self.path.read_text())
        self.assertEqual(saved["id"], "interface-new")
        self.assertEqual(saved["custom_root"], {"keep": True})
        self.assertEqual(saved["encryption"]["custom"], "keep")
        self.assertEqual(saved["clus"][0]["name"], "Main CLU")
        self.assertEqual(saved["clus"][0]["custom"], "keep")
        self.assertEqual(saved["clus"][1]["id"], "CUSTOM")
        self.assertEqual(saved["grenton_tracked_objects"], before)

    def test_update_tracked_objects_is_additive_and_deduplicated(self):
        _write_existing(self.path)

        save_configuration(_interface(), self.path, update_tracked_objects=True)

        tracked = yaml.safe_load(self.path.read_text())["grenton_tracked_objects"]["Main CLU"]
        self.assertEqual(tracked["custom_section"], "keep")
        self.assertEqual(
            tracked["variables"]["My custom variable group"],
            [{"name": "NewVariable", "description": "keep this", "sync": "slow"}],
        )
        self.assertEqual(
            tracked["variables"]["Component label"],
            [{"name": "ComponentVariable"}],
        )
        self.assertNotIn("Later duplicate label", tracked["variables"])
        self.assertEqual(
            tracked["attributes"]["My custom attribute group"],
            [{"object": "DIN1", "index": 0, "description": "keep this too"}],
        )
        self.assertEqual(
            tracked["attributes"]["First label"],
            [{"object": "DIN2", "index": 1, "description": "alsoState"}],
        )

    def test_update_tracked_objects_uses_first_label_for_new_identity(self):
        save_configuration(_interface(), self.path, update_tracked_objects=True)

        tracked = yaml.safe_load(self.path.read_text())["grenton_tracked_objects"]["Main CLU"]
        self.assertEqual(tracked["variables"]["First label"], [{"name": "NewVariable"}])
        self.assertEqual(
            tracked["variables"]["Component label"],
            [{"name": "ComponentVariable"}],
        )
        self.assertEqual(
            tracked["attributes"]["First label"],
            [
                {"object": "DIN1", "index": 0, "description": "state"},
                {"object": "DIN2", "index": 1, "description": "alsoState"},
            ],
        )

    def test_yaml_preserves_unicode_characters(self):
        interface = _interface()
        interface["name"] = "Sterowanie żółcią"
        interface["pages"][0]["widgets"][0]["label"] = "Żółć"

        save_configuration(interface, self.path, update_tracked_objects=True)

        serialized = self.path.read_text()
        self.assertIn("Sterowanie żółcią", serialized)
        self.assertIn("Żółć", serialized)
        self.assertNotIn("\\u", serialized)

    def test_attribute_identity_is_object_and_index_across_clus(self):
        interface = _interface()
        interface["clus"].append(
            {
                "id": "CLU2",
                "name": "Other CLU",
                "serialNumber": "456",
                "ip": "192.0.2.11",
                "port": 1234,
                "connectionType": "ETHERNET",
            }
        )
        interface["pages"][0]["widgets"].append(
            {
                "label": "Duplicate on another CLU",
                "text": {
                    "callType": "ATTRIBUTE",
                    "cluId": "CLU2",
                    "objectName": "DIN1",
                    "index": "0",
                },
            }
        )

        save_configuration(interface, self.path, update_tracked_objects=True)

        tracked = yaml.safe_load(self.path.read_text())["grenton_tracked_objects"]
        self.assertNotIn("Other CLU", tracked)


if __name__ == "__main__":
    unittest.main()
