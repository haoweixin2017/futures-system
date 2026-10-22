import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


# config 在导入时调用 load_config；屏蔽首次文件探测，避免读取工作区真实凭据。
with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
    Path, "exists", return_value=False
):
    from backend.common import config, security


class TestConfigCredentials(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.root = Path(self.contexts.enter_context(TemporaryDirectory()))
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.contexts.enter_context(mock.patch.dict(os.environ, {}, clear=True))
        self.contexts.enter_context(mock.patch.object(config, "ROOT_DIR", self.root))
        self.contexts.enter_context(
            mock.patch.object(config, "CONFIG_DIR", self.config_dir)
        )
        self.key_file = self.config_dir / "session_secret.key"
        self.contexts.enter_context(
            mock.patch.object(security, "SECRET_FILE", self.key_file)
        )
        settings = (
            "ctp:\n"
            '  user_id: "009001"\n'
            '  password: "fake-yaml-password"\n'
            '  broker_id: "0099"\n'
            '  app_id: "fake-yaml-app"\n'
            '  auth_code: "fake-yaml-auth"\n'
            '  md_address: "tcp://127.0.0.1:19001"\n'
            '  td_address: "tcp://127.0.0.1:19002"\n'
            "system:\n"
            '  feed: "simulator"\n'
            '  session_secret: "fake-yaml-session-secret"\n'
            "risk:\n"
            "  live_enabled: false\n"
        )
        for name in ("settings.yaml", "settings.simnow.yaml", "settings.prod.yaml"):
            (self.config_dir / name).write_text(settings, encoding="utf-8")

    def write_env(self, name, values, encoding="utf-8"):
        path = self.root / name
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding=encoding,
        )
        return path

    def test_profile_credentials_override_base_and_preserve_leading_zeroes(self):
        self.write_env(".env", {
            "FUTURES_PROFILE": "simnow",
            "FUTURES_CTP_USER_ID": "001001",
            "FUTURES_CTP_PASSWORD": "fake-base-password",
            "FUTURES_CTP_BROKER_ID": "0011",
            "FUTURES_CTP_APP_ID": "fake-base-app",
            "FUTURES_CTP_AUTH_CODE": "0000000000000011",
        })
        self.write_env(".env.simnow", {
            "FUTURES_CTP_USER_ID": "000202",
            "FUTURES_CTP_PASSWORD": "000303",
            "FUTURES_CTP_BROKER_ID": "0022",
            "FUTURES_CTP_APP_ID": "fake-profile-app",
            "FUTURES_CTP_AUTH_CODE": "0000000000000022",
        })

        loaded = config.load_config()

        self.assertEqual(loaded.system.profile, "simnow")
        self.assertEqual(loaded.ctp.user_id, "000202")
        self.assertEqual(loaded.ctp.password, "000303")
        self.assertEqual(loaded.ctp.broker_id, "0022")
        self.assertEqual(loaded.ctp.app_id, "fake-profile-app")
        self.assertEqual(loaded.ctp.auth_code, "0000000000000022")
        self.assertEqual(dict(os.environ), {})

    def test_base_credentials_override_yaml_when_profile_omits_them(self):
        self.write_env(".env", {
            "FUTURES_PROFILE": "simnow",
            "FUTURES_CTP_USER_ID": "000101",
            "FUTURES_CTP_PASSWORD": "fake-base-password",
        })
        self.write_env(".env.simnow", {"FUTURES_CTP_APP_ID": "fake-profile-app"})

        loaded = config.load_config()

        self.assertEqual(loaded.ctp.user_id, "000101")
        self.assertEqual(loaded.ctp.password, "fake-base-password")
        self.assertEqual(loaded.ctp.app_id, "fake-profile-app")
        self.assertEqual(loaded.ctp.auth_code, "fake-yaml-auth")
        self.assertEqual(dict(os.environ), {})

    def test_real_environment_has_highest_priority_including_profile(self):
        self.write_env(".env", {
            "FUTURES_PROFILE": "simnow",
            "FUTURES_CTP_USER_ID": "001001",
            "FUTURES_CTP_PASSWORD": "fake-base-password",
            "FUTURES_CTP_BROKER_ID": "0011",
            "FUTURES_CTP_APP_ID": "fake-base-app",
            "FUTURES_CTP_AUTH_CODE": "fake-base-auth",
        })
        self.write_env(".env.prod", {
            "FUTURES_CTP_USER_ID": "002002",
            "FUTURES_CTP_PASSWORD": "fake-profile-password",
            "FUTURES_CTP_BROKER_ID": "0022",
            "FUTURES_CTP_APP_ID": "fake-profile-app",
            "FUTURES_CTP_AUTH_CODE": "fake-profile-auth",
            "FUTURES_CTP_MD_ADDRESS": "tcp://127.0.0.1:19003",
        })
        external = {
            "FUTURES_PROFILE": "prod",
            "FUTURES_CTP_USER_ID": "000303",
            "FUTURES_CTP_PASSWORD": "fake-external-password",
            "FUTURES_CTP_BROKER_ID": "0033",
            "FUTURES_CTP_APP_ID": "fake-external-app",
            "FUTURES_CTP_AUTH_CODE": "0000000000000033",
        }
        with mock.patch.dict(os.environ, external, clear=True):
            loaded = config.load_config()
            self.assertEqual(loaded.system.profile, "prod")
            self.assertEqual(loaded.ctp.user_id, "000303")
            self.assertEqual(loaded.ctp.password, "fake-external-password")
            self.assertEqual(loaded.ctp.broker_id, "0033")
            self.assertEqual(loaded.ctp.app_id, "fake-external-app")
            self.assertEqual(loaded.ctp.auth_code, "0000000000000033")
            self.assertEqual(loaded.ctp.md_address, "tcp://127.0.0.1:19003")
            self.assertEqual(dict(os.environ), external)

    def test_empty_profile_authorization_does_not_fall_back_to_base_or_yaml(self):
        self.write_env(".env", {
            "FUTURES_PROFILE": "simnow",
            "FUTURES_CTP_APP_ID": "fake-base-app",
            "FUTURES_CTP_AUTH_CODE": "fake-base-auth",
        })
        self.write_env(".env.simnow", {
            "FUTURES_CTP_APP_ID": '""',
            "FUTURES_CTP_AUTH_CODE": "",
        })

        loaded = config.load_config()

        self.assertEqual(loaded.ctp.app_id, "")
        self.assertEqual(loaded.ctp.auth_code, "")
        self.assertEqual(dict(os.environ), {})

    def test_empty_external_authorization_overrides_profile_and_yaml(self):
        self.write_env(".env.local", {
            "FUTURES_CTP_APP_ID": "fake-profile-app",
            "FUTURES_CTP_AUTH_CODE": "fake-profile-auth",
        })
        external = {"FUTURES_CTP_APP_ID": "", "FUTURES_CTP_AUTH_CODE": "   "}
        with mock.patch.dict(os.environ, external, clear=True):
            loaded = config.load_config()
            self.assertEqual(loaded.ctp.app_id, "")
            self.assertEqual(loaded.ctp.auth_code, "")
            self.assertEqual(dict(os.environ), external)

    def test_switching_profiles_does_not_leak_credentials_or_mutate_environment(self):
        self.write_env(".env", {
            "FUTURES_CTP_USER_ID": "000101",
            "FUTURES_CTP_PASSWORD": "fake-base-password",
            "FUTURES_CTP_AUTH_CODE": "fake-base-auth",
        })
        self.write_env(".env.simnow", {
            "FUTURES_CTP_USER_ID": "000202",
            "FUTURES_CTP_PASSWORD": "fake-simnow-password",
            "FUTURES_CTP_AUTH_CODE": "fake-simnow-auth",
        })
        self.write_env(".env.prod", {
            "FUTURES_CTP_USER_ID": "000303",
            "FUTURES_CTP_PASSWORD": "fake-prod-password",
        })
        cases = (
            ("simnow", "000202", "fake-simnow-password", "fake-simnow-auth"),
            ("prod", "000303", "fake-prod-password", "fake-base-auth"),
            ("local", "000101", "fake-base-password", "fake-base-auth"),
            ("simnow", "000202", "fake-simnow-password", "fake-simnow-auth"),
        )
        for profile, user_id, password, auth_code in cases:
            with self.subTest(profile=profile):
                external = {"FUTURES_PROFILE": profile, "TEST_SENTINEL": "unchanged"}
                with mock.patch.dict(os.environ, external, clear=True):
                    loaded = config.load_config()
                    self.assertEqual(loaded.system.profile, profile)
                    self.assertEqual(loaded.ctp.user_id, user_id)
                    self.assertEqual(loaded.ctp.password, password)
                    self.assertEqual(loaded.ctp.auth_code, auth_code)
                    self.assertEqual(dict(os.environ), external)
        self.assertEqual(dict(os.environ), {})

    def test_repeated_loads_pick_up_file_changes_without_mutating_environment(self):
        for profile, user_id in (("simnow", "000101"), ("prod", "000202")):
            with self.subTest(profile=profile):
                self.write_env(".env", {
                    "FUTURES_PROFILE": profile,
                    "FUTURES_CTP_PASSWORD": f"fake-base-password-{profile}",
                })
                self.write_env(f".env.{profile}", {"FUTURES_CTP_USER_ID": user_id})
                for _ in range(2):
                    loaded = config.load_config()
                    self.assertEqual(loaded.system.profile, profile)
                    self.assertEqual(loaded.ctp.user_id, user_id)
                    self.assertEqual(loaded.ctp.password, f"fake-base-password-{profile}")
                    self.assertEqual(dict(os.environ), {})

    def test_read_dotenv_handles_bom_quotes_and_empty_values_without_side_effects(self):
        path = self.root / ".env"
        path.write_text(
            "FUTURES_PROFILE=simnow\n"
            "# 以下都是测试用假凭据\n"
            "\n"
            'export FUTURES_CTP_USER_ID="000101"\n'
            "FUTURES_CTP_PASSWORD='fake=password==value'\n"
            "FUTURES_CTP_APP_ID=\n"
            "FUTURES_CTP_AUTH_CODE=''\n"
            "ignored_without_equals\n",
            encoding="utf-8-sig",
        )

        parsed = config.read_dotenv(path)

        self.assertEqual(parsed, {
            "FUTURES_PROFILE": "simnow",
            "FUTURES_CTP_USER_ID": "000101",
            "FUTURES_CTP_PASSWORD": "fake=password==value",
            "FUTURES_CTP_APP_ID": "",
            "FUTURES_CTP_AUTH_CODE": "",
        })
        self.assertEqual(dict(os.environ), {})

    def test_load_config_reads_bom_in_base_and_profile_files(self):
        self.write_env(".env", {
            "FUTURES_PROFILE": "simnow",
            "FUTURES_CTP_USER_ID": "000101",
        }, encoding="utf-8-sig")
        self.write_env(".env.simnow", {
            "FUTURES_CTP_USER_ID": "000202",
        }, encoding="utf-8-sig")

        loaded = config.load_config()

        self.assertEqual(loaded.system.profile, "simnow")
        self.assertEqual(loaded.ctp.user_id, "000202")
        self.assertEqual(dict(os.environ), {})

    def test_invalid_profile_fails_from_base_or_real_environment(self):
        for source in ("base", "external"):
            for invalid in ("invalid-profile", "../prod", "prod/../../local"):
                with self.subTest(source=source, profile=invalid):
                    self.write_env(".env", {
                        "FUTURES_PROFILE": invalid if source == "base" else "simnow",
                    })
                    external = {"FUTURES_PROFILE": invalid} if source == "external" else {}
                    with mock.patch.dict(os.environ, external, clear=True):
                        with self.assertRaisesRegex(RuntimeError, "FUTURES_PROFILE"):
                            config.load_config()
                        self.assertEqual(dict(os.environ), external)

    def test_resolve_profile_uses_supplied_environment_including_empty_mapping(self):
        with mock.patch.dict(os.environ, {"FUTURES_PROFILE": "prod"}, clear=True):
            self.assertEqual(config.resolve_profile(), "prod")
            self.assertEqual(config.resolve_profile({}), "local")
            self.assertEqual(config.resolve_profile({"FUTURES_PROFILE": " SIMNOW "}), "simnow")
            with self.assertRaisesRegex(RuntimeError, "FUTURES_PROFILE"):
                config.resolve_profile({"FUTURES_PROFILE": "invalid-profile"})
            self.assertEqual(dict(os.environ), {"FUTURES_PROFILE": "prod"})

    def test_apply_env_overrides_uses_supplied_environment_not_process_values(self):
        supplied = {
            "FUTURES_CTP_USER_ID": "000101",
            "FUTURES_CTP_APP_ID": "fake-supplied-app",
            "FUTURES_CTP_AUTH_CODE": "0000000000000001",
        }
        external = {"FUTURES_CTP_USER_ID": "000999"}
        with mock.patch.dict(os.environ, external, clear=True):
            data = {"ctp": {"user_id": "fake-yaml-user"}}
            result = config.apply_env_overrides(data, supplied)
            self.assertEqual(result["ctp"], {
                "user_id": "000101",
                "app_id": "fake-supplied-app",
                "auth_code": "0000000000000001",
            })
            unchanged = {"ctp": {"user_id": "fake-yaml-user"}}
            self.assertEqual(config.apply_env_overrides(unchanged, {}), unchanged)
            self.assertEqual(unchanged["ctp"]["user_id"], "fake-yaml-user")
            self.assertEqual(dict(os.environ), external)

    def test_authorization_environment_keys_work_at_each_layer(self):
        for layer in ("base", "profile", "external"):
            with self.subTest(layer=layer):
                values = {
                    "FUTURES_CTP_APP_ID": f"fake-{layer}-app",
                    "FUTURES_CTP_AUTH_CODE": "0000000000000042",
                }
                self.write_env(".env", values if layer == "base" else {})
                self.write_env(".env.local", values if layer == "profile" else {})
                external = values if layer == "external" else {}
                with mock.patch.dict(os.environ, external, clear=True):
                    loaded = config.load_config()
                    self.assertEqual(loaded.ctp.app_id, f"fake-{layer}-app")
                    self.assertEqual(loaded.ctp.auth_code, "0000000000000042")
                    self.assertEqual(dict(os.environ), external)

    def test_all_ctp_fields_allow_explicit_empty_values(self):
        fields = ("user_id", "password", "broker_id", "app_id", "auth_code", "md_address", "td_address")
        data = {"ctp": {field: "fake-old-value" for field in fields}}
        supplied = {f"FUTURES_CTP_{field.upper()}": " " for field in fields}

        result = config.apply_env_overrides(data, supplied)

        self.assertEqual(result["ctp"], {field: "" for field in fields})
        self.assertEqual(dict(os.environ), {})

    def test_security_configured_secret_precedes_key_file_without_env_pollution(self):
        self.key_file.write_bytes(b"fake-key-file-secret\n")
        for layer in ("yaml", "base", "profile"):
            with self.subTest(layer=layer):
                base = {"FUTURES_PROFILE": "simnow"}
                if layer in ("base", "profile"):
                    base["FUTURES_SESSION_SECRET"] = "fake-base-session-secret"
                profile = (
                    {"FUTURES_SESSION_SECRET": "fake-profile-session-secret"}
                    if layer == "profile" else {}
                )
                self.write_env(".env", base)
                self.write_env(".env.simnow", profile)
                loaded = config.load_config()
                expected = f"fake-{layer}-session-secret".encode()
                self.assertEqual(loaded.system.session_secret.encode(), expected)
                with mock.patch.object(config, "cfg", loaded), mock.patch.object(
                    Path, "read_bytes", side_effect=AssertionError("不应读取低优先级 key 文件")
                ):
                    self.assertEqual(security._get_secret(), expected)
                self.assertEqual(dict(os.environ), {})
        self.assertEqual(self.key_file.read_bytes(), b"fake-key-file-secret\n")

    def test_security_external_secret_precedes_configured_secret_and_key_file(self):
        self.key_file.write_bytes(b"fake-key-file-secret")
        loaded = config.load_config()
        external = {"FUTURES_SESSION_SECRET": "  fake-external-session-secret  "}
        with mock.patch.object(config, "cfg", loaded), mock.patch.dict(
            os.environ, external, clear=True
        ), mock.patch.object(
            Path, "read_bytes", side_effect=AssertionError("不应读取低优先级 key 文件")
        ):
            self.assertEqual(loaded.system.session_secret, "fake-yaml-session-secret")
            self.assertEqual(security._get_secret(), b"fake-external-session-secret")
            self.assertEqual(dict(os.environ), external)

    def test_security_blank_external_secret_falls_back_to_configured_secret(self):
        self.key_file.write_bytes(b"fake-key-file-secret")
        loaded = config.load_config()
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)), mock.patch.object(
                config, "cfg", loaded
            ), mock.patch.dict(os.environ, {"FUTURES_SESSION_SECRET": blank}, clear=True):
                self.assertEqual(security._get_secret(), b"fake-yaml-session-secret")

    def test_security_key_file_is_used_when_environment_and_config_are_empty(self):
        self.key_file.write_bytes(b"  fake-key-file-secret\n")
        with mock.patch.object(config, "cfg", config.AppConfig()):
            self.assertEqual(security._get_secret(), b"fake-key-file-secret")
        self.assertEqual(dict(os.environ), {})


if __name__ == "__main__":
    unittest.main()
