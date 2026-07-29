"""Download policy: naming, skipping, hashing, and failure isolation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import requests
import responses

from conftest import make_settings
from spconnect.files import (
    FileDownloader,
    _unique_path,
    extension_of,
    filename_from_url,
    sanitise_filename,
)
from spconnect.transport import Transport

URL = "http://sp/sites/service/Lists/Cases/Attachments/1/foto.jpg"


@pytest.fixture
def rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


def downloader(tmp_path: Path, **overrides) -> FileDownloader:
    settings = make_settings(tmp_path, **overrides)
    return FileDownloader(Transport(settings), settings)


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (URL, "foto.jpg"),
        ("http://sp/Dokumente/Handbuch%20Stra%C3%9Fe.pdf", "Handbuch Straße.pdf"),
        ("http://sp/a/b/", "unnamed"),
        ("http://sp", "unnamed"),
        ("http://sp/a/re%2Fport.pdf", "re_port.pdf"),
    ],
)
def test_filename_from_url(url: str, expected: str) -> None:
    assert filename_from_url(url) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("normal.pdf", "normal.pdf"),
        ("a/b\\c:d*e?.txt", "a_b_c_d_e_.txt"),
        ("  .hidden.  ", "hidden"),
        ("", "unnamed"),
        ("Prüfprotokoll.pdf", "Prüfprotokoll.pdf"),
    ],
)
def test_sanitise_filename(raw: str, expected: str) -> None:
    assert sanitise_filename(raw) == expected


def test_long_names_are_truncated_but_keep_their_extension() -> None:
    name = sanitise_filename("x" * 300 + ".pdf")
    assert len(name) <= 120
    assert name.endswith(".pdf")


def test_long_names_without_a_usable_extension_are_simply_cut() -> None:
    name = sanitise_filename("y" * 300)
    assert len(name) == 120
    assert "." not in name


@pytest.mark.parametrize(
    ("name", "expected"), [("a.PDF", ".pdf"), ("a.tar.gz", ".gz"), ("noext", ""), (".env", ".env")]
)
def test_extension_of(name: str, expected: str) -> None:
    assert extension_of(name) == expected


def test_two_attachments_with_the_same_name_do_not_clobber(tmp_path: Path) -> None:
    (tmp_path / "foto.jpg").write_bytes(b"first")
    assert _unique_path(tmp_path, "foto.jpg").name == "foto(1).jpg"
    (tmp_path / "foto(1).jpg").write_bytes(b"second")
    assert _unique_path(tmp_path, "foto.jpg").name == "foto(2).jpg"


def test_unique_path_for_an_extensionless_name(tmp_path: Path) -> None:
    (tmp_path / "readme").write_bytes(b"x")
    assert _unique_path(tmp_path, "readme").name == "readme(1)"


# --------------------------------------------------------------------------- #
# downloading
# --------------------------------------------------------------------------- #


def test_download_streams_hashes_and_sizes(rsps, tmp_path: Path) -> None:
    payload = b"JPEG-ish bytes" * 100
    rsps.add(responses.GET, URL, body=payload, status=200)

    record = downloader(tmp_path).download(URL, tmp_path / "files" / "1")

    assert record.downloaded is True
    assert record.filename == "foto.jpg"
    assert record.bytes == len(payload)
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert record.skip_reason is None
    assert Path(record.local_path).read_bytes() == payload


def test_explicit_filename_wins_over_the_url(rsps, tmp_path: Path) -> None:
    rsps.add(responses.GET, URL, body=b"x", status=200)
    record = downloader(tmp_path).download(URL, tmp_path / "f", "Handbuch Straße.pdf")
    assert record.filename == "Handbuch Straße.pdf"


def test_downloads_disabled(tmp_path: Path) -> None:
    record = downloader(tmp_path, download_files=False).download(URL, tmp_path / "f")
    assert record.downloaded is False
    assert record.skip_reason == "downloads_disabled"
    assert not (tmp_path / "f").exists()


def test_excluded_extension_is_skipped_before_any_request(tmp_path: Path) -> None:
    record = downloader(tmp_path, skip_extensions=".jpg,.exe").download(URL, tmp_path / "f")
    assert record.skip_reason == "extension_excluded:.jpg"


def test_content_length_over_the_limit_skips_without_reading_the_body(rsps, tmp_path: Path) -> None:
    rsps.add(responses.GET, URL, body=b"x" * 5000, status=200)
    record = downloader(tmp_path, max_file_mb=0.001).download(URL, tmp_path / "f")
    assert record.skip_reason.startswith("too_large")
    assert record.downloaded is False


def test_oversize_is_caught_mid_stream_when_content_length_lies(rsps, tmp_path: Path) -> None:
    rsps.add(responses.GET, URL, body=b"x" * 5000, status=200, headers={"Content-Length": "nonsense"})
    record = downloader(tmp_path, max_file_mb=0.001).download(URL, tmp_path / "f")
    assert record.skip_reason.startswith("too_large")
    assert not list((tmp_path / "f").iterdir())  # the partial file is removed


def test_a_failed_download_is_recorded_not_raised(rsps, tmp_path: Path) -> None:
    rsps.add(responses.GET, URL, status=404)
    record = downloader(tmp_path).download(URL, tmp_path / "f")
    assert record.downloaded is False
    assert record.skip_reason == "download_failed:NotFoundError"


def test_a_connection_reset_mid_stream_is_recorded(rsps, tmp_path: Path) -> None:
    rsps.add(responses.GET, URL, body=requests.ConnectionError("boom"))
    record = downloader(tmp_path, max_retries=1).download(URL, tmp_path / "f")
    assert record.skip_reason.startswith("download_failed")
    assert record.downloaded is False


def test_skip_reason_is_none_for_an_acceptable_file(tmp_path: Path) -> None:
    assert downloader(tmp_path).skip_reason("foto.jpg") is None
    assert downloader(tmp_path).skip_reason("foto.jpg", size=10) is None
    assert downloader(tmp_path).skip_reason("foto.jpg", size=10**12).startswith("too_large")
