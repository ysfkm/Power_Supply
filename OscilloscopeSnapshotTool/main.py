"""Entry point for the power supply snapshot tool's GUI."""

from gui import SnapshotApp


def main():
    app = SnapshotApp()
    app.mainloop()


if __name__ == "__main__":
    main()
