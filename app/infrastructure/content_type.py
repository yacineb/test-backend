"""Magic-byte content sniffing, via puremagic.

puremagic rather than python-magic: the runtime image is distroless, with no
shell and no package manager, so a binding to the system libmagic shared object
would have to be vendored in by hand. puremagic is pure Python with no
dependencies of its own and needs nothing from the base image.
"""

import puremagic

from app.domain.ports import ContentTypeDetector


class PureMagicDetector(ContentTypeDetector):
    def sniff(self, head: bytes) -> str | None:
        try:
            return puremagic.from_string(head, mime=True)
        except puremagic.PureError, ValueError:
            # Unrecognised, or too few bytes to tell. Both mean "not something
            # we can vouch for", which the caller treats the same way.
            return None
