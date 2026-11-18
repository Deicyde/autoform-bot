"""Find the links, statement cards and formulas a site's build broke.

``mkdocs build --strict`` never checks a link written as raw HTML, and it logs a
broken Markdown anchor only at info level. Render writes many links as raw HTML,
and a Markdown conversion has dropped statement cards with every check green.
Nor does the build typeset a formula, which a reader may then see in red.
This reads the built site itself, and reports what it finds as warnings.
"""

from __future__ import annotations

import html
import json
import posixpath
import re
import stat
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .. import lean_probe
from .._fs import path_is
from ..markdown import FORMULA_CLASS
from ..tex_macros import TEX_MACROS
from .graph_pages import SEARCH_INDEX
from .mathjax import CARD_CLASS, MATHJAX_SCRIPT, MATHJAX_VERSION
from .project_map import MAP_DIRECTORY, SCHEMA as MAP_SCHEMA
from .publishable import _TEX_DEFINITION
from .site_index import SEARCH_SCHEMA, _openable_route

#: The attribute through which each element links to, or loads, another file.
_LINK_ATTRIBUTES = {
    "a": "href",
    "area": "href",
    "link": "href",
    "audio": "src",
    "embed": "src",
    "iframe": "src",
    "img": "src",
    "script": "src",
    "source": "src",
    "track": "src",
    "video": "src",
}

#: The opening tag render writes for each statement card, on a line of its own.
_SOURCE_CARD = re.compile(r'^<div class="bp-thmwrapper\b[^"]*"[^>]*?\bdata-autoform-node-id="([^"]*)"', re.MULTILINE)

#: How long Node.js may take to typeset a site's formulas: a start, and a share
#: for each distinct formula over ten times what MathJax took on a CI runner,
#: which typeset the 74,592 of the ATLAS wiki in 30 s.
_TYPESET_START = 30.0
_TYPESET_PER_FORMULA = 0.005
#: The script that typesets them.
_TYPESET_SCRIPT = Path(__file__).with_name("typeset.cjs")
#: Where the settings render wrote start in ``javascripts/mathjax.js``.
_SETTINGS = "var SETTINGS = "
#: How much of a formula a warning quotes.
_QUOTED = 80


class SiteCheckError(ValueError):
    """The site or its source cannot be read as a directory."""


@dataclass(frozen=True, slots=True)
class SiteReport:
    pages: int
    issues: tuple[str, ...]


@dataclass(slots=True)
class _Page:
    ids: set[str] = field(default_factory=set)
    links: list[str] = field(default_factory=list)
    cards: list[str] = field(default_factory=list)
    #: The text of each formula the articles' MathJax input typesets.
    formulas: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Open:
    """An element the parser is in: its tag, how many of that tag are open in it, and its text."""

    tag: str
    depth: int = 1
    text: list[str] = field(default_factory=list)


class _PageParser(HTMLParser):
    def __init__(self, page: _Page) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self._card: _Open | None = None
        self._formula: _Open | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values: dict[str, str] = {}
        for name, value in attrs:
            # A browser keeps the first of two equal attribute names.
            values.setdefault(name, value or "")
        if "id" in values:
            self.page.ids.add(values["id"])
        if tag == "a" and "name" in values:
            self.page.ids.add(values["name"])
        attribute = _LINK_ATTRIBUTES.get(tag)
        if attribute is not None and attribute in values:
            self.page.links.append(values[attribute])
        classes = values.get("class", "").split()
        if tag == "div" and "bp-thmwrapper" in classes and "data-autoform-node-id" in values:
            self.page.cards.append(values["data-autoform-node-id"])
        for opened in (self._card, self._formula):
            if opened is not None and opened.tag == tag:
                opened.depth += 1
        # The articles' input typesets each marked formula outside a read-back card, as its text reads.
        if self._card is None and CARD_CLASS in classes:
            self._card = _Open(tag)
        elif self._card is None and self._formula is None and FORMULA_CLASS in classes:
            self._formula = _Open(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for opened in (self._card, self._formula):
            if opened is not None and opened.tag == tag:
                opened.depth -= 1
        if self._formula is not None and not self._formula.depth:
            self.page.formulas.append("".join(self._formula.text))
            self._formula = None
        if self._card is not None and not self._card.depth:
            self._card = None

    def handle_data(self, data: str) -> None:
        if self._formula is not None:
            self._formula.text.append(data)


def _is_directory(path: Path) -> bool:
    """Whether ``path`` is a directory; one that cannot be examined is refused as unreadable, not missing."""

    try:
        return path_is(path, stat.S_ISDIR)
    except OSError as error:
        raise SiteCheckError(f"{path} cannot be read: {error.strerror}") from error


def check_site(
    site_dir: str | Path, source_dir: str | Path | None = None, mathjax_dir: str | Path | None = None
) -> SiteReport:
    """Report the internal links, explorer data, fragments and statement cards
    a built site lacks, and the formulas it cannot typeset.

    Every HTML page under *site_dir* is read once. A link with a scheme or a
    network location, or a path from the site root, depends on where the site
    is served and is not checked. With *source_dir*, the rendered Markdown the
    site was built from, each source page's statement cards must all be on the
    page built from it. With *mathjax_dir*, an unpacked MathJax 3.2.2 package,
    each article formula is typeset as :func:`_formula_problems` says.
    """

    site = Path(site_dir)
    if not _is_directory(site):
        raise SiteCheckError(f"{site} is not a directory")
    source = None if source_dir is None else Path(source_dir)
    if source is not None and not _is_directory(source):
        raise SiteCheckError(f"{source} is not a directory")
    issues: list[str] = []
    pages: dict[str, _Page] = {}
    for path in sorted(site.rglob("*.html")):
        if not path.is_file():
            continue
        name = path.relative_to(site).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            issues.append(f"{name}: could not be read: {error}")
            continue
        page = _Page()
        parser = _PageParser(page)
        parser.feed(text)
        parser.close()
        pages[name] = page
    files: dict[str, str | None] = {}
    # A missing file many pages link to, such as a shared stylesheet, is reported once, with a count.
    missing: dict[str, tuple[int, set[str]]] = {}
    for name, page in pages.items():
        for link in page.links:
            problem = _link_problem(site, pages, files, name, link)
            if problem is None:
                continue
            message, target = problem
            if target in missing:
                missing[target][1].add(name)
                continue
            if target is not None:
                missing[target] = (len(issues), {name})
            issues.append(f"{name}: {message}")
    for index, linking in missing.values():
        if len(linking) > 1:
            issues[index] += f"; {len(linking) - 1} other page(s) link to it too"
    if not pages:
        issues.append("the site contains no HTML pages")
    issues.extend(_explorer_problems(site, source, pages, files))
    if source is not None:
        issues.extend(_card_problems(source, pages))
    if mathjax_dir is not None:
        issues.extend(_formula_problems(site, Path(mathjax_dir), pages))
    # Render repeats a body link in a hidden validation copy, so one broken link can appear twice.
    return SiteReport(len(pages), tuple(dict.fromkeys(issues)))


def _explorer_payload(site: Path, name: str, schema: str, issues: list[str]) -> dict | None:
    try:
        data = json.loads((site / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(f"{name}: missing explorer data")
        return None
    except (OSError, ValueError, RecursionError) as error:
        issues.append(f"{name}: could not read explorer data: {error}")
        return None
    if not isinstance(data, dict) or data.get("schema") != schema:
        issues.append(f"{name}: expected explorer data with schema {schema}")
        return None
    return data


def _explorer_problems(
    site: Path, source: Path | None, pages: dict[str, _Page], files: dict[str, str | None],
) -> list[str]:
    """Check known renderer files, using the same bases as the explorer.

    Search links are relative to the search index. Map summaries name a shard
    basename relative to the map's parts directory, even inside another shard.
    Each shard is read at most once, and only if a summary names it.
    """
    issues: list[str] = []

    def expected(name: str) -> bool:
        for root in (site, source):
            if root is None:
                continue
            path = root / name
            try:
                if path_is(path, lambda _: True, follow_symlinks=False):
                    return True
            except OSError as error:
                raise SiteCheckError(f"{path} cannot be read: {error.strerror}") from error
        return False

    if expected(SEARCH_INDEX):
        index = _explorer_payload(site, SEARCH_INDEX, SEARCH_SCHEMA, issues)
        if index is not None:
            rows = index.get("nodes")
            if not isinstance(rows, list):
                issues.append(f"{SEARCH_INDEX}: expected a list of nodes")
            else:
                for row in rows:
                    if not isinstance(row, dict):
                        issues.append(f"{SEARCH_INDEX}: invalid node")
                        continue
                    route = row.get("route")
                    destination = route if _openable_route(route) else row.get("url")
                    for link in (destination, row.get("data_url")):
                        if isinstance(link, str) and (problem := _link_problem(site, pages, files, SEARCH_INDEX, link)):
                            issues.append(f"{SEARCH_INDEX}: {problem[0]}")

    map_index = f"{MAP_DIRECTORY}/index.json"
    if not expected(map_index):
        return issues
    index = _explorer_payload(site, map_index, MAP_SCHEMA, issues)
    if index is None:
        return issues
    regions = index.get("regions")
    if not isinstance(regions, list):
        return [*issues, f"{map_index}: expected a list of regions"]

    pending = [(map_index, region) for region in reversed(regions)]
    seen: set[tuple[str, str]] = set()
    shards: dict[str, dict | None] = {}
    while pending:
        name, summary = pending.pop()
        part = summary.get("id") if isinstance(summary, dict) else None
        basename = summary.get("file") if isinstance(summary, dict) else None
        if (
            not isinstance(part, str) or not part
            or not isinstance(basename, str) or re.fullmatch(r"[0-9a-f]{2}\.json", basename) is None
        ):
            issues.append(f"{name}: invalid map part id or shard name")
            continue
        shard = f"{MAP_DIRECTORY}/parts/{basename}"
        key = (shard, part)
        if key in seen:
            continue
        seen.add(key)
        if shard not in shards:
            data = _explorer_payload(site, shard, MAP_SCHEMA, issues)
            parts = data.get("parts") if data is not None else None
            if data is not None and not isinstance(parts, dict):
                issues.append(f"{shard}: expected a map of parts")
            shards[shard] = parts if isinstance(parts, dict) else None
        parts = shards[shard]
        if parts is None:
            continue
        body = parts.get(part)
        if not isinstance(body, dict):
            issues.append(f"{name}: map part {part!r} is missing from {shard}")
            continue
        children = body.get("children")
        if not isinstance(children, list):
            issues.append(f"{shard}: expected a list of children for map part {part!r}")
            continue
        pending.extend((shard, child) for child in reversed(children))
    return issues


def _link_problem(
    site: Path, pages: dict[str, _Page], files: dict[str, str | None], page: str, link: str
) -> tuple[str, str | None] | None:
    """What is wrong with *link* on *page*, and the missing site path when that is the problem."""

    try:
        parts = urlsplit(link.strip())
    except ValueError:
        return f"{link} is not a valid URL", None
    if parts.scheme or parts.netloc or parts.path.startswith("/"):
        return None
    target = page
    if parts.path:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(page), unquote(parts.path)))
        if joined == ".." or joined.startswith("../"):
            return f"{link} leaves the site", None
        if joined not in files:
            files[joined] = _published_file(site, joined)
        found = files[joined]
        if found is None:
            return f"{link} names {joined}, which the site does not contain", joined
        target = found
    fragment = unquote(parts.fragment)
    # A fragment of name=value pairs, such as the dependency explorer's #node=, is read by the page's script.
    if fragment and "=" not in parts.fragment and target in pages and fragment not in pages[target].ids:
        return f"{link} names no anchor #{fragment} on {target}", None
    return None


def _published_file(site: Path, path: str) -> str | None:
    """The file a site path serves: the file itself, or a directory's ``index.html``."""

    if (site / path).is_dir():
        path = posixpath.normpath(posixpath.join(path, "index.html"))
    return path if (site / path).is_file() else None


def _card_problems(source: Path, pages: dict[str, _Page]) -> list[str]:
    issues: list[str] = []
    for path in sorted(source.rglob("*.md")):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            issues.append(f"{relative}: could not be read: {error}")
            continue
        expected = [html.unescape(node) for node in _SOURCE_CARD.findall(text)]
        if not expected:
            continue
        built = _built_page(relative, pages)
        if built is None:
            issues.append(f"{relative}: no built page holds its {len(expected)} statement card(s)")
            continue
        present = set(pages[built].cards)
        missing = [node for node in expected if node not in present]
        if missing:
            issues.append(
                f"{built}: {len(missing)} of the {len(expected)} statement card(s) in {relative} "
                f"did not survive the build: {', '.join(missing)}"
            )
    return issues


def _formula_problems(site: Path, mathjax: Path, pages: dict[str, _Page]) -> list[str]:
    """What MathJax 3.2.2, the package at *mathjax*, makes of each article
    formula, with the settings the site's ``javascripts/mathjax.js`` gives it.

    Node.js typesets each distinct formula alone, with a new TeX state, as the
    commands the site turns off make each formula on a page stand alone. A
    formula MathJax rejects is one warning, with how many other pages have it;
    a command it does not know is one warning, with its first formula and page,
    how many formulas and pages use it, and the fix. Check names the turned-off
    commands and the definitions no page package loads, so they are left to it.
    What stops the typesetting, no settings, package or Node.js, the time limit
    or a crash, is one warning.
    """

    found: dict[str, dict[str, None]] = {}
    for name, page in pages.items():
        for formula in page.formulas:
            found.setdefault(formula, {})[name] = None
    if not found:
        return []
    unchecked = f"so the site's {len(found)} distinct formula(s) were not typeset"
    settings = _page_settings(site)
    if settings is None:
        return [f"{MATHJAX_SCRIPT}: holds no settings render wrote for MathJax {MATHJAX_VERSION}, {unchecked}"]
    if not _is_mathjax(mathjax):
        return [f"{mathjax} is not an unpacked MathJax {MATHJAX_VERSION} package, {unchecked}"]
    formulas = list(found)
    limit = _TYPESET_START + _TYPESET_PER_FORMULA * len(formulas)
    try:
        with tempfile.TemporaryDirectory(prefix="autoform-typeset-") as scratch:
            job = Path(scratch, "job.json")
            job.write_text(json.dumps({"settings": settings, "formulas": formulas}), encoding="utf-8")
            arguments = [str(_TYPESET_SCRIPT), str(mathjax.resolve()), str(job)]
            output = lean_probe.run_node(arguments, cwd=job.parent, timeout=limit)
    except (lean_probe.SkeletonError, OSError) as error:
        return [f"{error}, {unchecked}"]
    # Each formula the script could not typeset: the pages it is on, the TeX MathJax found in it, and the error
    # or the commands MathJax did not know.
    try:
        failed = [
            (
                found[formulas[result["formula"]]],
                (bool(result["display"]), str(result["tex"])),
                result.get("error"),
                [str(command) for command in result.get("undefined", [])],
            )
            for result in json.loads(output)
        ]
    except (ValueError, LookupError, TypeError, AttributeError):
        return [f"MathJax wrote no results site check can read, {unchecked}"]
    # Each warning stands where its first formula does, and its counts are added once every formula is read.
    issues: list[str] = []
    rejected: dict[tuple[bool, str], tuple[int, dict[str, None]]] = {}
    unknown: dict[str, tuple[int, set[tuple[bool, str]], dict[str, None]]] = {}
    for on, formula, error, undefined in failed:
        first = next(iter(on))
        if error is not None:
            if formula not in rejected:
                rejected[formula] = (len(issues), {})
                issues.append(f"{first}: MathJax cannot typeset {_quoted(*formula)}: {error}")
            rejected[formula][1].update(on)
            continue
        for command in undefined:
            if command == "\\notallowed" or _TEX_DEFINITION.fullmatch(command):
                continue
            if command not in unknown:
                unknown[command] = (len(issues), set(), {})
                shown = _quoted(*formula)
                issues.append(f"{first}: {command} is undefined, so the site shows it in red, first in {shown}")
            unknown[command][1].add(formula)
            unknown[command][2].update(on)
    for index, having in rejected.values():
        if len(having) > 1:
            issues[index] += f"; {len(having) - 1} other page(s) have it too"
    for index, using, having in unknown.values():
        issues[index] += f"; it is in {len(using)} formula(s) on {len(having)} page(s): define it in {TEX_MACROS}"
    return issues


def _page_settings(site: Path) -> dict[str, object] | None:
    """The settings ``javascripts/mathjax.js`` gives the articles' TeX input, or
    None when the site has no such script render wrote for MathJax 3.2.2."""

    try:
        script = (site / MATHJAX_SCRIPT).read_text(encoding="utf-8")
        settings = json.JSONDecoder().raw_decode(script, script.index(_SETTINGS) + len(_SETTINGS))[0]
    except (OSError, ValueError):
        return None
    page = settings.get("page") if isinstance(settings, dict) and settings.get("version") == MATHJAX_VERSION else None
    return page if isinstance(page, dict) else None


def _is_mathjax(package: Path) -> bool:
    """Whether *package* is an unpacked MathJax 3.2.2 package, as npm's tarball unpacks one."""

    try:
        version = json.loads((package / "package.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return False
    return version == MATHJAX_VERSION and (package / "es5" / "node-main.js").is_file()


def _quoted(display: bool, tex: str) -> str:
    """*tex* as a warning quotes it: on one line, shortened, between the dollars a page would write."""

    text = " ".join(tex.split())
    if len(text) > _QUOTED:
        text = text[: _QUOTED - 3] + "..."
    return f"$${text}$$" if display else f"${text}$"


def _built_page(relative: str, pages: dict[str, _Page]) -> str | None:
    """The page MkDocs builds from a source page, with or without directory URLs."""

    stem = posixpath.splitext(relative)[0]
    directory, name = posixpath.split(stem)
    candidates = [stem + ".html", posixpath.join(stem, "index.html")]
    if name in {"README", "index"}:
        candidates.insert(0, posixpath.join(directory, "index.html"))
    return next((candidate for candidate in candidates if candidate in pages), None)


__all__ = ["SiteCheckError", "SiteReport", "check_site"]
