"""Module for directory modification watching."""

from pathlib import Path
from typing import Callable

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer


class DirectoryWatcher:
    """A directory watcher class."""

    def __init__(
        self,
        directory: Path,
        on_modified: Callable[[Path], None],
    ) -> None:
        """
        Initialize a watcher which observes modifications in `directory`.

        on_modified: A callable which is invoked with the path of modified
                     files within `directory`.
        """
        self.on_modified = on_modified
        self.watched_directory = str(directory)
        self.observer = Observer()

    def start(self) -> None:
        """Start watching the specified directory for file modifications."""
        event_handler = DirectoryEventHandler(self.on_modified)
        self.observer.schedule(
            event_handler,
            self.watched_directory,
            recursive=True,
        )
        self.observer.start()

    def stop(self) -> None:
        """Stop watching the directory."""
        if self.observer.is_alive():
            try:
                self.observer.stop()
                self.observer.join()
            except RuntimeError:
                # TODO: Understand exactly what join() does, and why
                # it sometimes throws a RuntimeError
                pass


class DirectoryEventHandler(FileSystemEventHandler):
    """An event handler for filesystem changes within a directory."""

    def __init__(self, on_modified: Callable[[Path], None]) -> None:
        """Initialize event handler with callback functions."""
        self._on_modified = on_modified

    def on_modified(self, event: FileModifiedEvent) -> None:
        """Call on_modified callback function on modifed event in dir."""
        if event.is_directory:
            return

        self._on_modified(Path(event.src_path).absolute())
