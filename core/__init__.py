import os

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_provider(key, script_dir=SCRIPT_DIR):
    if key == "onedrive":
        from .providers.onedrive import OneDriveProvider
        return OneDriveProvider(script_dir)
    if key == "gdrive_api":
        from .providers.gdrive_api import GDriveApiProvider
        return GDriveApiProvider(script_dir)
    if key == "gdrive_public":
        from .providers.gdrive_public import GDrivePublicProvider
        return GDrivePublicProvider()
    raise ValueError(f"Unknown provider: {key}")
