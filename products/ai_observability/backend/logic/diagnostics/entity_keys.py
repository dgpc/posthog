import re
import base64
import binascii

# An error message or a tool name is free text, so it travels in a URL as the unpadded base64url of its UTF-8 bytes,
# the way trace ids do.
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


class MalformedEntityKeyError(ValueError):
    pass


def encode_entity_key(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def decode_entity_key(key: str) -> str:
    if not _BASE64URL.fullmatch(key):
        raise MalformedEntityKeyError(key)
    try:
        value = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise MalformedEntityKeyError(key) from error
    # The decoder ignores unused trailing bits, so a second key would otherwise name the same value.
    if encode_entity_key(value) != key:
        raise MalformedEntityKeyError(key)
    return value
