import ast
import unittest
from pathlib import Path

from pydantic import ValidationError

from comet.core.models import AppSettings
from comet.core.operator_settings import BOOTSTRAP_SETTING_KEYS, LiveSettings
from comet.core.settings_catalog import build_settings_catalog
from comet.core.settings_policy import (
    COMPONENT_SETTING_KEYS,
    PROCESS_RESTART_SETTING_KEYS,
    apply_mode,
    settings_requiring_restart,
)
from comet.observability.logging import LoggingSettings

ROOT = Path(__file__).parents[1]
ALLOWED_MODULE_CAPTURES = {
    "comet/api/app.py",
    "comet/api/endpoints/stream.py",
    "comet/core/models.py",
}


class LiveSettingsTests(unittest.TestCase):
    def test_bound_work_keeps_one_generation(self):
        first = AppSettings(_env_file=None, ADDON_NAME="first")
        second = AppSettings(_env_file=None, ADDON_NAME="second")
        live = LiveSettings(first)

        with live.bind():
            live.publish(second)
            self.assertEqual(live.ADDON_NAME, "first")

        self.assertEqual(live.ADDON_NAME, "second")

    def test_application_policy_sets_are_valid_and_disjoint(self):
        catalog_keys = {entry.key for entry in build_settings_catalog()}
        self.assertTrue(PROCESS_RESTART_SETTING_KEYS <= catalog_keys)
        self.assertTrue(COMPONENT_SETTING_KEYS <= catalog_keys)
        self.assertTrue(BOOTSTRAP_SETTING_KEYS <= catalog_keys)
        self.assertTrue(PROCESS_RESTART_SETTING_KEYS.isdisjoint(COMPONENT_SETTING_KEYS))
        self.assertTrue(PROCESS_RESTART_SETTING_KEYS.isdisjoint(BOOTSTRAP_SETTING_KEYS))
        self.assertTrue(COMPONENT_SETTING_KEYS.isdisjoint(BOOTSTRAP_SETTING_KEYS))
        self.assertTrue(all(apply_mode(key) for key in catalog_keys))
        with self.assertRaises(KeyError):
            apply_mode("UNDECLARED_SETTING")

    def test_usenet_enablement_transition_keeps_dependent_changes_pending(self):
        changed = (
            "USENET_ENABLED",
            "USENET_ENGINE_ENABLED",
            "USENET_ENGINE_REQUIRED",
            "SCRAPE_ANIMETOSHO_USENET",
            "HTTP_CLIENT_LIMIT",
        )

        self.assertEqual(
            settings_requiring_restart(changed),
            {
                "USENET_ENABLED",
                "USENET_ENGINE_ENABLED",
                "USENET_ENGINE_REQUIRED",
                "SCRAPE_ANIMETOSHO_USENET",
            },
        )

    def test_usenet_engine_can_reload_when_enablement_is_already_applied(self):
        self.assertEqual(settings_requiring_restart({"USENET_ENGINE_ENABLED"}), set())

    def test_published_models_reject_in_place_mutation(self):
        application = AppSettings(_env_file=None)
        logging = LoggingSettings(_env_file=None)

        with self.assertRaises(ValidationError):
            application.ADDON_NAME = "mutated"
        with self.assertRaises(ValidationError):
            logging.LOG_PROFILE = "debug"

    def test_live_settings_are_not_captured_or_written_by_business_modules(self):
        captures = []
        writes = []
        for path in (ROOT / "comet").rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {
                child: parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "settings"
                    and node.attr.isupper()
                ):
                    continue
                parent = parents.get(node)
                if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = (
                        parent.targets
                        if isinstance(parent, ast.Assign)
                        else [parent.target]
                    )
                    if node in targets:
                        writes.append(f"{relative}:{node.lineno}")
                scope = node
                while scope in parents:
                    scope = parents[scope]
                    if isinstance(
                        scope,
                        (
                            ast.FunctionDef,
                            ast.AsyncFunctionDef,
                            ast.ClassDef,
                            ast.Lambda,
                        ),
                    ):
                        break
                else:
                    if relative not in ALLOWED_MODULE_CAPTURES:
                        captures.append(f"{relative}:{node.lineno}")

        self.assertEqual(writes, [])
        self.assertEqual(captures, [])
