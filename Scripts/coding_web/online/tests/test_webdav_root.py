"""The WebDAV folder URL for a Nextcloud host, a Nextcloud DAV URL and a generic
``/webdav`` root such as LRZ Sync+Share (PowerFolder)."""
from online.nextcloud import webdav_root


def test_nextcloud_host_gets_the_files_path():
    assert (webdav_root("https://cloud.example.org", "anna", "Coding/meta")
            == "https://cloud.example.org/remote.php/dav/files/anna/Coding/meta")


def test_nextcloud_dav_url_is_used_as_given():
    u = "https://cloud.example.org/remote.php/dav/files/anna"
    assert webdav_root(u, "anna", "meta") == u + "/meta"
    assert webdav_root(u + "/meta/", "anna", "meta") == u + "/meta"


def test_sync_and_share_webdav_root():
    # PowerFolder: the first segment below /webdav is the shared folder's name
    assert (webdav_root("https://syncandshare.lrz.de/webdav/", "ab12cde@campus.lmu.de", "Meta analysis")
            == "https://syncandshare.lrz.de/webdav/Meta%20analysis")
    assert (webdav_root("https://syncandshare.lrz.de/webdav/Meta%20analysis", "x", "Meta analysis")
            == "https://syncandshare.lrz.de/webdav/Meta%20analysis")
    assert (webdav_root("https://syncandshare.lrz.de/webdav", "x", "Team/meta")
            == "https://syncandshare.lrz.de/webdav/Team/meta")


def test_public_share_link_root_is_used_as_given():
    """A public share link (D11): /public.php/dav/files/<token> is the WebDAV root.

    Basic auth there is ``anonymous`` + the link password, so the owner's user
    name must never end up in the URL - the root stands on its own.
    """
    u = "https://cloud.example.org/public.php/dav/files/AbC123token"
    assert webdav_root(u, "anonymous") == u
    assert webdav_root(u + "/", "anonymous") == u
    assert webdav_root(u, "anonymous", "sub") == u + "/sub"
    # a plain host still becomes the owner's dav path
    assert (webdav_root("https://cloud.example.org", "anna", "Coding")
            == "https://cloud.example.org/remote.php/dav/files/anna/Coding")
