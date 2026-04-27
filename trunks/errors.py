class TrunksError(Exception):
    """Base exception for Trunks."""


class InvalidPath(TrunksError, ValueError):
    pass


class ObjectNotFound(TrunksError, KeyError):
    pass


class RefConflict(TrunksError):
    pass


class RepositoryNotFound(TrunksError):
    pass


class RepositoryCorrupt(TrunksError):
    pass


class BackendUnavailable(TrunksError):
    pass


class GitNotFound(TrunksError):
    pass


class UnsupportedGitCommand(TrunksError):
    pass
