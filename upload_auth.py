"""One-use capability required by the shared public-upload function."""


class UploadApprovalRequiredError(PermissionError):
    """Raised when a public upload has no matching owner approval."""


_SEAL = object()


class UploadApproval:
    __slots__ = ("clip_id", "owner_id", "_seal", "_used")

    def __init__(self, clip_id, owner_id, seal):
        if seal is not _SEAL:
            raise TypeError("Upload approvals can only be issued by the owner confirmation flow.")
        self.clip_id = str(clip_id)
        self.owner_id = str(owner_id)
        self._seal = seal
        self._used = False

    def consume(self, clip_id):
        if self._seal is not _SEAL or self._used or self.clip_id != str(clip_id):
            raise UploadApprovalRequiredError("A valid, unused owner confirmation is required for this clip.")
        self._used = True


def issue_upload_approval(clip_id, owner_id):
    """Issue an in-process, one-use grant after Telegram owner confirmation."""
    return UploadApproval(clip_id, owner_id, _SEAL)


def consume_upload_approval(approval, clip_id):
    if not isinstance(approval, UploadApproval):
        raise UploadApprovalRequiredError("Public upload requires owner confirmation through the Telegram bot.")
    approval.consume(clip_id)
