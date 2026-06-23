from abc import ABC, abstractmethod


class Provider(ABC):
    """A download source. Subclasses either stream per file (supports_streaming
    = True and implement get_download) or do their own bulk download
    (supports_streaming = False and implement bulk_download)."""

    key = "base"
    label = "Base"
    supports_streaming = True

    @abstractmethod
    def authenticate(self):
        """Block until ready to scan/download, or raise on failure."""

    @abstractmethod
    def scan(self, source, on_entry=None):
        """Return a list of file entries:
        {status, size, rel_path, item_id, extra}. on_entry(entry) is called for
        every file as it is discovered (used for live counts / stream download)."""

    def get_download(self, entry):
        """Return (url, headers) for streaming this entry. Streaming providers
        only."""
        raise NotImplementedError

    def bulk_download(self, manifest, dest, progress, stop_event=None, on_tick=None, log=None):
        """Download everything in the manifest, updating progress in place.
        Non-streaming providers only."""
        raise NotImplementedError
