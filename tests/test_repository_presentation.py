"""Offline checks for the curated landing pages, navigation, and artwork.

This is deliberately not a checkout crawler or a general Markdown parser. Only
the named public documents below are opened; no runtime, credentials, deployment
notes, network, application service, SSH transport, or model is involved.
PNG structure checks preserve ancillary metadata (including C2PA/caBX); they do
not claim that valid artwork is metadata-free or perform any image edits.
"""
from __future__ import annotations

from collections import Counter
from html import unescape
from pathlib import Path
import re
import struct
import unittest
from urllib.parse import unquote, urlsplit
import zlib


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    "README.md", "README.zh-CN.md", "CONTRIBUTING.md",
    "docs/index.md", "docs/repository-layout.md",
    "docs/guides/operator-guide.md", "docs/guides/operator-guide.zh-CN.md",
    "docs/guides/visual-demo.md", "assets/brand/README.md",
    "assets/brand/generation.md",
)
# Anchors may point to these other, explicitly public reference documents.
# Do not follow links recursively or read arbitrary root-level Markdown files.
ANCHOR_DOCUMENTS = frozenset(DOCUMENTS) | {
    "SECURITY.md", "docs/agent-workbench.md", "docs/background-service.md",
    "docs/management-session-product.md", "docs/codex-maintenance.md",
    "docs/ssh-transport.md", "docs/rebranding.md",
}
PUBLIC_ROOT_FILES = {
    "README.md", "README.zh-CN.md", "CONTRIBUTING.md", "SECURITY.md",
    "LICENSE", "THIRD_PARTY_NOTICES.md",
}
MARK = "assets/brand/agentcaptain-mark.png"
WORKSPACE = "docs/images/workspace-demo.jpg"
CAPTAIN = "docs/images/captain-demo.jpg"
MAX_ASSET_BYTES = 2 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
AGENT_SETUP_INTRO = (
    "If you have a coding agent (Claude Code, Cursor, Codex, whatever), "
    "you don't need to read the rest of this file. Just say to it:"
)


def without_fences(text: str) -> str:
    """Ignore triple-backtick/tilde examples, retaining ordinary prose only."""
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def local_links(text: str) -> list[str]:
    """Recognize inline Markdown and HTML links/images, not bare URLs."""
    prose = without_fences(text)
    targets = re.findall(r"\]\(([^\s)]+)\)", prose)
    targets += re.findall(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", prose, re.I)
    targets += re.findall(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"']", prose, re.I)
    return [unescape(target) for target in targets
            if not urlsplit(target).scheme and not target.startswith("//")]


def heading_anchors(text: str) -> set[str]:
    """Small GFM-style heading slugger for the plain headings used here."""
    anchors = set()
    counts: Counter[str] = Counter()
    for line in without_fences(text).splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            # The landing hero uses a native HTML heading so title and logo
            # share one GitHub-compatible centered container.
            match = re.match(r"^\s*<h[1-6]\b[^>]*>(.*?)</h[1-6]>\s*$", line, re.I)
            if not match:
                continue
        heading = re.sub(r"<[^>]+>", "", unescape(match.group(1))).lower()
        slug = re.sub(r"[^\w -]", "", heading).replace(" ", "-")
        duplicate = counts[slug]
        counts[slug] += 1
        anchors.add(f"{slug}-{duplicate}" if duplicate else slug)
    return anchors


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Validate bounded PNG chunk framing/CRC and return its IHDR dimensions.

    Unknown ancillary chunks are accepted, not stripped. This does not decode
    pixels, prove visual quality, or certify embedded provenance assertions.
    """
    if len(data) >= MAX_ASSET_BYTES or not data.startswith(PNG_MAGIC):
        raise ValueError("Expected a PNG smaller than 2 MiB")
    offset = len(PNG_MAGIC)
    chunks = []
    dimensions = None
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError("Truncated PNG chunk header")
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("Truncated PNG chunk payload")
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise ValueError("PNG chunk CRC mismatch")
        if not chunks:
            if kind != b"IHDR" or length != 13:
                raise ValueError("PNG must start with a complete IHDR")
            dimensions = struct.unpack_from(">II", payload)
            if not all(dimensions):
                raise ValueError("PNG dimensions must be positive")
        elif kind == b"IHDR":
            raise ValueError("Duplicate PNG IHDR")
        chunks.append(kind)
        offset = end
        if kind == b"IEND":
            if length or offset != len(data) or b"IDAT" not in chunks:
                raise ValueError("Incomplete PNG or data after IEND")
            return dimensions
    raise ValueError("Missing PNG IEND")


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Check native JPEG framing and bounded segment headers, not pixel data."""
    if (len(data) >= MAX_ASSET_BYTES or not data.startswith(b"\xff\xd8\xff")
            or not data.endswith(b"\xff\xd9")):
        raise ValueError("Expected a complete JPEG smaller than 2 MiB")
    offset = 2
    dimensions = None
    # SOF markers contain precision, height, width; C4/C8/CC are other tables.
    frame_markers = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
    while offset < len(data) - 2:
        if data[offset] != 0xFF:
            raise ValueError("Invalid JPEG segment marker")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            raise ValueError("Truncated JPEG marker")
        marker = data[offset]
        offset += 1
        if marker in {0xDA, 0xD9}:  # Stop before entropy-coded scan data.
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data) - 2:
            raise ValueError("Truncated JPEG segment length")
        length = struct.unpack_from(">H", data, offset)[0]
        if length < 2 or offset + length > len(data) - 2:
            raise ValueError("Truncated JPEG segment payload")
        if marker in frame_markers:
            if length < 8:
                raise ValueError("Incomplete JPEG frame header")
            height, width = struct.unpack_from(">HH", data, offset + 3)
            dimensions = (width, height)
        offset += length
    if dimensions is None or not all(dimensions):
        raise ValueError("Missing positive JPEG frame dimensions")
    return dimensions


class RepositoryPresentationTests(unittest.TestCase):
    def test_curated_document_links_and_heading_anchors_exist(self):
        for relative in DOCUMENTS:
            source = ROOT / relative
            content = source.read_text(encoding="utf-8")
            for link in local_links(content):
                with self.subTest(document=relative, link=link):
                    parts = urlsplit(link)
                    target = (source.parent / unquote(parts.path)).resolve() if parts.path else source
                    self.assertTrue(target.is_relative_to(ROOT), "Link escapes the source checkout")
                    path = target.relative_to(ROOT).as_posix()
                    self.assertTrue(path in PUBLIC_ROOT_FILES or path.startswith(("docs/", "assets/")),
                                    "Link must stay in the public documentation/asset namespace")
                    self.assertNotIn(".runtime", target.relative_to(ROOT).parts)
                    self.assertTrue(target.is_file(), "Missing local link target")
                    if parts.fragment:
                        self.assertIn(path, ANCHOR_DOCUMENTS, "Anchor target must be explicitly public")
                        anchors = heading_anchors(target.read_text(encoding="utf-8"))
                        self.assertIn(unquote(parts.fragment), anchors, "Missing heading anchor")

    def test_bilingual_landing_pages_share_the_canonical_assets(self):
        for relative in ("README.md", "README.zh-CN.md"):
            with self.subTest(document=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                links = local_links(content)
                self.assertIn(MARK, links)
                self.assertIn(WORKSPACE, links)
                images = re.findall(r"<img\b[^>]*\bsrc=\"([^\"]+)\"", content)
                self.assertEqual([src for src in images if not urlsplit(src).scheme], [MARK])
                self.assertIn("<h1>AgentCaptain</h1>", content)
                self.assertIn("docs/guides/visual-demo.md", links)

    def test_landing_hero_groups_logo_title_tagline_badges_and_navigation(self):
        for relative in ("README.md", "README.zh-CN.md"):
            with self.subTest(document=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertTrue(content.startswith('<div align="center">\n'))
                hero, body = content.split("</div>\n", 1)
                self.assertEqual(re.findall(r"<h1\b[^>]*>(.*?)</h1>", hero), ["AgentCaptain"])
                self.assertIn(MARK, local_links(hero))
                self.assertIn("One captain for agents across your machines.", hero)
                self.assertIn('alt="CI"', hero)
                self.assertIn("docs/index.md", local_links(hero))
                self.assertRegex(body, r"^\s*!\[AgentCaptain")
                self.assertNotIn("<div", body, "The centered hero must not swallow the body")

    def test_public_product_name_has_no_translated_alias(self):
        for relative in sorted(ANCHOR_DOCUMENTS):
            with self.subTest(document=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("\u961f\u957f", content)

    def test_agent_setup_prompt_precedes_manual_steps_and_keeps_human_boundaries(self):
        cases = (
            ("README.md", "## Get started", "### 🤖 Let your agent set it up", "### Manual setup",
             ("README", "operator guide", "verify", "existing Codex", "SSH", "sessions", "login", "trust", "approval", "URL", "delegating")),
            ("README.zh-CN.md", "## 快速开始", "### 🤖 让你的 Agent 帮你安装", "### 手动安装",
             ("README", "操作指南", "验证", "现有 Codex", "SSH", "会话", "登录", "信任", "审批", "地址", "派发")),
        )
        for relative, started, heading, manual, requirements in cases:
            with self.subTest(document=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                section = content.split(started + "\n", 1)[1]
                self.assertTrue(section.lstrip().startswith(heading))
                prompt_section, manual_section = section.split(manual + "\n", 1)
                self.assertIn(AGENT_SETUP_INTRO, prompt_section)
                prompt = re.search(r"```text\n(.*?)\n```", prompt_section, re.S)
                self.assertIsNotNone(prompt, "Installation instructions should be a copyable plain-text prompt")
                text = prompt.group(1)
                self.assertIn("https://github.com/du-du-wang/agent-captain", text)
                for requirement in requirements:
                    self.assertIn(requirement, text)
                self.assertIn("npm ci", manual_section)
                self.assertIn("python scripts/captain.py serve", manual_section)

    def test_superseded_logo_files_are_not_shipped_or_referenced(self):
        old_assets = ("assets/agent-captain-logo.svg", "web/agent-captain-mark.svg",
                      "assets/brand/agentcaptain-mark.svg")
        for old in old_assets:
            self.assertFalse((ROOT / old).exists(), old)
        for relative in DOCUMENTS:
            content = (ROOT / relative).read_text(encoding="utf-8")
            for old in old_assets:
                self.assertNotIn(old.rsplit("/", 1)[-1], content, relative)

    def test_canonical_logo_is_a_bounded_square_png(self):
        width, height = png_dimensions((ROOT / MARK).read_bytes())
        self.assertEqual(width, height)
        self.assertGreaterEqual(width, 512)
        self.assertLessEqual(width, 2048)

    def test_workspace_screenshot_is_a_bounded_native_landscape_jpeg(self):
        self.assert_landscape_screenshot(WORKSPACE)

    def test_captain_screenshot_is_a_bounded_native_landscape_jpeg(self):
        self.assert_landscape_screenshot(CAPTAIN)

    def assert_landscape_screenshot(self, relative: str):
        width, height = jpeg_dimensions((ROOT / relative).read_bytes())
        self.assertGreaterEqual(width, 1024)
        self.assertLessEqual(width, 4096)
        self.assertGreaterEqual(height, 500)
        self.assertGreater(width, height)

    def test_link_extractor_covers_html_anchors_and_ignores_fences_external_urls(self):
        sample = ("[local](guide.md#heading) <img src='mark.png' alt='mark'>\n"
                  "[web](https://example.test/no-network) <https://example.test/bare>\n"
                  "```sh\n[example](not-a-real-file.md)\n```\n"
                  "~~~text\n<img src='not-a-real-image.png'>\n~~~\n"
                  "<placeholder> https://example.test/plain\n"
                  "<a href='guide.md#html&amp;anchor'>HTML navigation</a>\n"
                  "<a href='https://example.test/outside'>external</a>\n")
        self.assertEqual(local_links(sample), ["guide.md#heading", "mark.png", "guide.md#html&anchor"])

    def test_heading_slugs_cover_current_unicode_and_duplicate_titles(self):
        self.assertEqual(heading_anchors("<h1>AgentCaptain</h1>\n# 环境要求\n## Optional: connect\n## Optional: connect\n"),
                         {"agentcaptain", "环境要求", "optional-connect", "optional-connect-1"})

    def test_png_validator_rejects_truncation_and_crc_damage(self):
        original = (ROOT / MARK).read_bytes()
        damaged = bytearray(original)
        damaged[16] ^= 1
        for invalid in (b"not a PNG", original[:-1], bytes(damaged), original + b"trailing"):
            with self.subTest(bytes=len(invalid)), self.assertRaises(ValueError):
                png_dimensions(invalid)

    def test_png_validator_accepts_ancillary_metadata_without_stripping_it(self):
        original = (ROOT / MARK).read_bytes()
        # This checks framing tolerance only, not validity of C2PA assertions.
        kind, payload = b"caBX", b"synthetic ancillary metadata regression fixture"
        chunk = (struct.pack(">I", len(payload)) + kind + payload
                 + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
        with_ancillary = original[:-12] + chunk + original[-12:]
        before = bytes(with_ancillary)
        self.assertEqual(png_dimensions(with_ancillary), png_dimensions(original))
        self.assertEqual(with_ancillary, before)

    def test_jpeg_validator_rejects_wrong_format_and_incomplete_files(self):
        original = (ROOT / WORKSPACE).read_bytes()
        for invalid in (b"not a JPEG", original[:-1], original[:16] + b"\xff\xd9"):
            with self.subTest(bytes=len(invalid)), self.assertRaises(ValueError):
                jpeg_dimensions(invalid)


if __name__ == "__main__":
    unittest.main()
