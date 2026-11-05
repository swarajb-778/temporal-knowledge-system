"""Repository guards that run in CI.

OWASP AST08 (Poor Scanning): pattern scanners alone miss things, so these checks are structural (AST-level
import/call inspection) plus content scans over exactly the files git would publish.
"""
import ast
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBMISSION = ROOT / "submission"
SNAPSHOTS = next((p for p in (Path(os.environ.get("TKS_SNAPSHOTS", ROOT / "snapshots")),
                               ROOT.parent / "hackathon-knowledge-kit-participant" / "snapshots")
                  if (p / "05_2026-09-28_latest").is_dir()), ROOT / "snapshots")

# OWASP AST03 (Over-Privileged Skills): the answering code needs no network, processes, dynamic code or
# serialization formats that execute on load. Anything here is a privilege it must not quietly gain.
BANNED_IMPORTS = {"socket", "subprocess", "urllib.request", "http", "http.client", "requests", "httpx", "pickle",
                  "marshal", "shelve", "ctypes", "multiprocessing", "yaml"}
BANNED_BUILTINS = {"eval", "exec", "compile", "__import__", "open"}     # bare calls; re.compile is fine
BANNED_OS_CALLS = {"system", "popen", "spawnl", "spawnv", "execv", "execl", "remove", "unlink", "rmdir"}
SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|"
                    r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
                    r"(?i:api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"])")


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True)
    return [ROOT / p for p in out.stdout.splitlines() if (ROOT / p).is_file()]


class TestSubmissionPrivileges(unittest.TestCase):
    def test_no_network_process_or_dynamic_code(self):
        for path in SUBMISSION.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    names = []
                for n in names:
                    self.assertFalse(n in BANNED_IMPORTS or n.split(".")[0] in BANNED_IMPORTS, f"{path}: import {n}")
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Name):
                        self.assertNotIn(fn.id, BANNED_BUILTINS, f"{path}:{node.lineno} calls {fn.id}() - the "
                                         "submission must only read what ingest() is given")
                    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "os":
                        self.assertNotIn(fn.attr, BANNED_OS_CALLS, f"{path}:{node.lineno} calls os.{fn.attr}")

    def test_no_hardcoded_public_answers(self):
        """The hidden set rewords the public one; answers must come from the data, not from the source."""
        source = "\n".join(p.read_text() for p in SUBMISSION.rglob("*.py"))
        literals = set(re.findall(r"\d+\.\d+%?|\d{5,}", source))
        for line in (ROOT / "eval" / "public_eval.jsonl").read_text().splitlines():
            for v in json.loads(line).get("must_include", []):
                if re.fullmatch(r"\$?\d[\d,.]*%?", v) and len(v) >= 4:
                    self.assertNotIn(v.replace(",", ""), literals, f"public answer {v} hard-coded")


class TestPublishedFiles(unittest.TestCase):
    def test_no_secrets(self):
        for path in tracked_files():
            if path.suffix in (".py", ".md", ".json", ".jsonl", ".txt", ".yml", ".yaml", ".toml", ".cfg", ""):
                self.assertIsNone(SECRET.search(path.read_text(errors="ignore")), f"possible secret in {path}")

    def test_only_project_files_are_published(self):
        allowed = {".github", ".gitignore", "NOTES.md", "README.md", "eval", "evals", "harness", "requirements.txt",
                   "submission", "tests"}
        for path in tracked_files():
            rel = path.relative_to(ROOT).as_posix()
            self.assertFalse(rel.endswith((".zip", ".pdf")), f"kit archive or document tracked: {rel}")
            self.assertIn(rel.split("/")[0], allowed, f"unexpected top-level file tracked: {rel}")

    @unittest.skipUnless((SNAPSHOTS / "05_2026-09-28_latest").is_dir(), "snapshots not available")
    def test_no_snapshot_text_redistributed(self):
        """The website text is licensed for the event only: no published file may carry page prose."""
        sample = set()
        for page in sorted((SNAPSHOTS / "05_2026-09-28_latest" / "pages").glob("*.md"))[::7]:
            for line in page.read_text(errors="ignore").splitlines():
                line = line.strip()
                if len(line) > 90 and not line.startswith(("|", "[", "!", "http")):
                    sample.add(line[:90])
        published = "\n".join(p.read_text(errors="ignore") for p in tracked_files() if p.suffix != ".zip")
        leaked = [s for s in sample if s in published]
        self.assertEqual(leaked, [], f"{len(leaked)} snapshot lines found in tracked files")


if __name__ == "__main__":
    unittest.main()
