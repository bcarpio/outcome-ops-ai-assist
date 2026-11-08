"""Unit tests for the Go code-map backend (process-repo-summary)."""

import importlib.util
import os
import sys

import pytest

# Load base + go_backend modules directly (mirror test_java_backend.py).
base_path = os.path.join(
    os.path.dirname(__file__), "../../process-repo-summary/backends/base.py"
)
spec = importlib.util.spec_from_file_location("backend_base", base_path)
base_module = importlib.util.module_from_spec(spec)
sys.modules["backend_base"] = base_module
sys.modules["backends.base"] = base_module
spec.loader.exec_module(base_module)

go_backend_path = os.path.join(
    os.path.dirname(__file__), "../../process-repo-summary/backends/go_backend.py"
)
spec = importlib.util.spec_from_file_location("go_backend", go_backend_path)
go_backend_module = importlib.util.module_from_spec(spec)
sys.modules["go_backend"] = go_backend_module
spec.loader.exec_module(go_backend_module)

CodeUnit = base_module.CodeUnit
GoBackend = go_backend_module.GoBackend


def _blob(path):
    return {"type": "blob", "path": path, "name": path.split("/")[-1]}


# gin-like file tree: packages at root + subdirs, tests beside source.
GIN_FILES = [
    _blob("gin.go"),
    _blob("context.go"),
    _blob("context_test.go"),
    _blob("binding/binding.go"),
    _blob("binding/json.go"),
    _blob("binding/form_mapping_test.go"),
    _blob("render/render.go"),
    _blob("internal/json/json.go"),
    _blob("internal/bytesconv/bytesconv.go"),
    _blob("go.mod"),
    _blob("go.sum"),
    _blob("README.md"),
    _blob("vendor/github.com/x/y/z.go"),   # excluded
    _blob("testdata/fixture.go"),          # excluded
]


class TestGoBackendBasics:
    def test_backend_type_and_name(self):
        b = GoBackend({"github_token": "t"})
        assert b.get_backend_type() == "go"
        assert b.get_backend_name() == "Go Backend"

    def test_source_extensions(self):
        assert GoBackend({"github_token": "t"}).get_source_extensions() == {".go"}

    def test_validate_requires_token(self):
        ok, _ = GoBackend({"github_token": "t"}).validate_config()
        assert ok is True
        bad_ok, msg = GoBackend({}).validate_config()
        assert bad_ok is False
        assert "github_token" in msg


class TestGoPackageDiscovery:
    @pytest.fixture
    def units(self):
        return GoBackend({"github_token": "t"}).discover_code_units(GIN_FILES)

    def _by_name(self, units, batch_type):
        return {
            u.name: u for u in units
            if u.metadata.get("batch_type") == batch_type
        }

    def test_each_directory_is_its_own_package(self, units):
        mods = self._by_name(units, "source-module")
        # root package + binding + render + two separate internal subpackages
        assert "root" in mods
        assert "binding" in mods
        assert "render" in mods
        assert "internal-json" in mods
        assert "internal-bytesconv" in mods

    def test_parent_and_child_not_merged(self, units):
        mods = self._by_name(units, "source-module")
        # internal/json and internal/bytesconv are DISTINCT units, not rolled
        # into a single "internal" unit.
        assert "internal" not in mods
        assert set(mods["internal-json"].file_paths) == {"internal/json/json.go"}
        assert set(mods["internal-bytesconv"].file_paths) == {
            "internal/bytesconv/bytesconv.go"
        }

    def test_root_package_collects_root_go_files_only(self, units):
        mods = self._by_name(units, "source-module")
        assert set(mods["root"].file_paths) == {"gin.go", "context.go"}

    def test_test_files_excluded_from_source_and_grouped(self, units):
        mods = self._by_name(units, "source-module")
        for u in mods.values():
            assert not any(p.endswith("_test.go") for p in u.file_paths)
        tests = self._by_name(units, "tests")
        assert "unit" in tests
        assert set(tests["unit"].file_paths) == {
            "context_test.go",
            "binding/form_mapping_test.go",
        }

    def test_build_unit_has_go_mod(self, units):
        build = self._by_name(units, "build")
        assert "build-config" in build
        assert set(build["build-config"].file_paths) == {"go.mod", "go.sum"}

    def test_vendor_and_testdata_excluded(self, units):
        all_files = [p for u in units for p in u.file_paths]
        assert not any("vendor/" in p for p in all_files)
        assert not any("testdata/" in p for p in all_files)


class TestGoBatchMetadata:
    def test_storage_key_for_module(self):
        b = GoBackend({"github_token": "t"})
        u = CodeUnit("binding", "source-module", ["binding/json.go"],
                     {"batch_type": "source-module"})
        assert b.get_storage_key(u) == "summary#module#binding"

    def test_batch_metadata_backend_type(self):
        b = GoBackend({"github_token": "t"})
        u = CodeUnit("binding", "source-module", ["binding/json.go"],
                     {"batch_type": "source-module"})
        meta = b.generate_batch_metadata(u, "gin")
        assert meta["backend_type"] == "go"
        assert meta["group_name"] == "binding"
        assert meta["files"] == [{"path": "binding/json.go"}]
