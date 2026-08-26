from __future__ import annotations

import unittest

from codex_usage.domain.git_remote import (
    RemoteNormalizationError,
    RemoteResolutionKind,
    normalize_remote,
    resolve_remote,
)


class NormalizeRemoteTests(unittest.TestCase):
    def test_github_transport_credentials_case_and_suffix_are_removed(self) -> None:
        variants = (
            "https://token@github.com/Owner/Repository.git",
            "ssh://git@github.com:22/Owner/Repository.git",
            "git@github.com:Owner/Repository.git",
            "git://github.com/Owner/Repository.git",
        )

        for remote in variants:
            with self.subTest(remote=remote):
                self.assertEqual(
                    normalize_remote(remote),
                    "github.com/owner/repository",
                )

    def test_query_fragment_repeated_slashes_and_trailing_slash_are_removed(self) -> None:
        remote = "https://github.com//Owner//Repository.git/?token=x#fragment"

        self.assertEqual(
            normalize_remote(remote),
            "github.com/owner/repository",
        )

    def test_non_github_path_case_is_preserved(self) -> None:
        remote = "https://GitLab.Example.com/Group/SubGroup/Repository.git"

        self.assertEqual(
            normalize_remote(remote),
            "gitlab.example.com/Group/SubGroup/Repository",
        )

    def test_non_default_port_is_preserved_and_ipv6_is_bracketed(self) -> None:
        self.assertEqual(
            normalize_remote("ssh://git@git.example.com:2222/Group/Repo.git"),
            "git.example.com:2222/Group/Repo",
        )
        self.assertEqual(
            normalize_remote("ssh://git@[2001:db8::1]:2222/Group/Repo.git"),
            "[2001:db8::1]:2222/Group/Repo",
        )

    def test_local_remotes_return_none(self) -> None:
        local_remotes = (
            "file:///C:/repos/example.git",
            "C:\\repos\\example.git",
            "C:relative-repo.git",
            "/home/user/example.git",
            "../example.git",
            "example.git",
            "\\\\server\\share\\example.git",
        )

        for remote in local_remotes:
            with self.subTest(remote=remote):
                self.assertIsNone(normalize_remote(remote))

    def test_empty_unsupported_and_dot_segment_remotes_are_rejected(self) -> None:
        invalid_remotes = (
            "",
            "ftp://example.com/group/repo.git",
            "https://example.com/group/../repo.git",
            "https://example.com/.git",
        )

        for remote in invalid_remotes:
            with self.subTest(remote=remote):
                with self.assertRaises(RemoteNormalizationError):
                    normalize_remote(remote)


class ResolveRemoteTests(unittest.TestCase):
    def test_network_origin_wins_over_other_remotes(self) -> None:
        result = resolve_remote(
            "git@github.com:Owner/Main.git",
            ["https://gitlab.example.com/Group/Mirror.git"],
        )

        self.assertEqual(result.kind, RemoteResolutionKind.ORIGIN)
        self.assertEqual(result.canonical, "github.com/owner/main")

    def test_one_non_origin_network_remote_is_selected(self) -> None:
        result = resolve_remote(
            None,
            ["https://gitlab.example.com/Group/Repository.git"],
        )

        self.assertEqual(result.kind, RemoteResolutionKind.UNIQUE_REMOTE)
        self.assertEqual(
            result.canonical,
            "gitlab.example.com/Group/Repository",
        )

    def test_transport_variants_of_same_remote_are_not_ambiguous(self) -> None:
        result = resolve_remote(
            None,
            [
                "git@github.com:Owner/Repository.git",
                "https://github.com/owner/repository.git",
            ],
        )

        self.assertEqual(result.kind, RemoteResolutionKind.UNIQUE_REMOTE)
        self.assertEqual(result.candidates, ("github.com/owner/repository",))

    def test_multiple_distinct_network_remotes_are_ambiguous(self) -> None:
        result = resolve_remote(
            None,
            [
                "https://github.com/example/one.git",
                "https://github.com/example/two.git",
            ],
        )

        self.assertEqual(result.kind, RemoteResolutionKind.AMBIGUOUS_REMOTE)
        self.assertIsNone(result.canonical)
        self.assertEqual(len(result.candidates), 2)

    def test_local_origin_can_fall_back_to_one_network_remote(self) -> None:
        result = resolve_remote(
            "C:\\repos\\local.git",
            ["C:\\repos\\local.git", "https://github.com/example/repo.git"],
        )

        self.assertEqual(result.kind, RemoteResolutionKind.UNIQUE_REMOTE)
        self.assertEqual(result.canonical, "github.com/example/repo")

    def test_only_local_remotes_are_local_only(self) -> None:
        result = resolve_remote(None, ["../repository.git"])

        self.assertEqual(result.kind, RemoteResolutionKind.LOCAL_ONLY)
        self.assertIsNone(result.canonical)

    def test_no_remotes_are_unclassified(self) -> None:
        result = resolve_remote(None, [])

        self.assertEqual(result.kind, RemoteResolutionKind.UNCLASSIFIED)
        self.assertIsNone(result.canonical)


if __name__ == "__main__":
    unittest.main()
