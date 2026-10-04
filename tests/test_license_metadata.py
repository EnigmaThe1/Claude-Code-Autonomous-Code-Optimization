# Copyright 2026 Bogdan Carp (@EnigmaThe1)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COPYRIGHT = "Copyright 2026 Bogdan Carp (@EnigmaThe1)"
APACHE_MARKER = 'Licensed under the Apache License, Version 2.0 (the "License");'


def test_apache_license_and_notice_are_present():
    license_text = (ROOT / "LICENSE").read_text()
    notice_text = (ROOT / "NOTICE").read_text()
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert COPYRIGHT in notice_text
    assert "Apache License, Version 2.0" in notice_text


def test_project_owned_runtime_sources_carry_apache_header():
    paths = [ROOT / "install.sh", ROOT / "uninstall.sh", ROOT / "bin" / "claude-auto"]
    paths.extend(sorted((ROOT / "lib").glob("*.py")))
    paths.extend(sorted((ROOT / "hooks").glob("*.py")))
    paths.append(ROOT / "scripts" / "build_manifest.py")
    for path in paths:
        header = "\n".join(path.read_text().splitlines()[:24])
        assert COPYRIGHT in header, path
        assert APACHE_MARKER in header, path


def test_manifest_and_installer_include_license_and_notice():
    manifest = (ROOT / "MANIFEST.sha256").read_text()
    assert "  LICENSE\n" in manifest
    assert "  NOTICE\n" in manifest
    assert "  scripts/build_manifest.py\n" in manifest
    installer = (ROOT / "install.sh").read_text()
    assert installer.count('"LICENSE"') >= 2
    assert installer.count('"NOTICE"') >= 2
    assert installer.count('"scripts"') >= 2
