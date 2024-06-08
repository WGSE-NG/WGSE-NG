import filecmp
from pathlib import Path
from helix.configuration import RepositoryConfig
from helix.data.genome import Genome
from helix.files.downloader import Downloader
from test.create_dirs import remote_repo
from test.utility import MockPath


def test_downloaded_file_content_matches(remote_repo, tmp_path):
    cfg = RepositoryConfig()
    cfg.temporary = tmp_path

    downloader = Downloader(cfg)
    sut = downloader.perform(remote_repo["bgzip"]["genome"])

    assert filecmp.cmp(sut, remote_repo["bgzip"]["file_on_disk"])


def test_genome_md5_is_assigned_correctly(remote_repo, tmp_path):
    cfg = RepositoryConfig()
    cfg.temporary = tmp_path

    downloader = Downloader(cfg)
    sut = downloader.perform(remote_repo["bgzip"]["genome"])

    assert remote_repo["bgzip"]["md5"] == remote_repo["bgzip"]["genome"].downloaded_md5


def test_size_fetch_is_correct(remote_repo, tmp_path):
    cfg = RepositoryConfig()
    cfg.temporary = tmp_path

    downloader = Downloader(cfg)
    sut = downloader.perform(remote_repo["bgzip"]["genome"])

    assert remote_repo["bgzip"]["size"] == remote_repo["bgzip"]["genome"].download_size


def test_download_is_resumed_correctly(remote_repo, tmp_path):
    cfg = RepositoryConfig()
    cfg.temporary = tmp_path
    genome = remote_repo["bgzip"]["genome"]
    temporary_file = tmp_path.joinpath(genome.name_only)

    ten_bytes = remote_repo["bgzip"]["file_on_disk"].read_bytes()[0:10]
    temporary_file.write_bytes(ten_bytes)

    downloader = Downloader(cfg)
    sut = downloader.perform(remote_repo["bgzip"]["genome"])

    assert filecmp.cmp(sut, remote_repo["bgzip"]["file_on_disk"])


def test_already_downloaded_file_is_skipped(remote_repo, tmp_path):
    cfg = RepositoryConfig()
    cfg.temporary = tmp_path
    genome = remote_repo["bgzip"]["genome"]
    temporary_file = tmp_path.joinpath(genome.name_only)

    file = remote_repo["bgzip"]["file_on_disk"].read_bytes()
    temporary_file.write_bytes(file)
    last_edit = temporary_file.stat().st_mtime_ns

    downloader = Downloader(cfg)
    Path().stat().st_mtime_ns
    sut = downloader.perform(remote_repo["bgzip"]["genome"])

    assert sut.stat().st_mtime_ns == last_edit
