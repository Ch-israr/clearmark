"""Credit maths for ClearMark.

Pricing (from the approved plan):
- first image removal: free (one per account)
- image: 5 credits
- audio: 5 credits per started minute
- video: 15 credits per started second

Credits are reserved when an upload is accepted and charged when the
job succeeds. A failed job refunds the reservation, so a failed job is
never charged.
"""

import math

COST_IMAGE = 5
COST_AUDIO_PER_MINUTE = 5
COST_VIDEO_PER_SECOND = 15


def cost_for(media, duration_seconds=None):
    """Credits one removal costs. duration_seconds is required for
    audio (minutes) and video (seconds); images are a flat price."""
    if media == "image":
        return COST_IMAGE
    if media == "audio":
        seconds = duration_seconds or 0
        minutes = max(1, math.ceil(seconds / 60))
        return COST_AUDIO_PER_MINUTE * minutes
    if media == "video":
        seconds = duration_seconds or 0
        return COST_VIDEO_PER_SECOND * max(1, math.ceil(seconds))
    raise ValueError(f"unknown media type: {media}")


def price_for(user, media, duration_seconds=None):
    """Return (cost, used_trial) for this user and media type. The free
    trial applies to the first image removal only."""
    if media == "image" and not user.get("free_used"):
        return 0, True
    return cost_for(media, duration_seconds), False


def can_afford(user, cost):
    return user.get("credits", 0) >= cost
