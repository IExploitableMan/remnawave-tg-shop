from typing import Any, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from bot.utils.config_link import prepare_config_links
from config.settings import Settings
from db.dal import user_dal


async def prepare_paid_config_links(
    settings: Settings,
    session: AsyncSession,
    user_id: int,
    i18n: Optional[Any],
    lang: str,
    raw_config_link: Optional[str],
) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Prepare paid VPN config links, suppressing them when required channel access
    is not verified yet. This closes the payment-success message bypass path.
    """
    if await is_paid_config_link_blocked(settings, session, user_id):
        text = (
            i18n.gettext(lang, "config_link_requires_channel_subscription")
            if i18n
            else "VPN link will be available after channel subscription verification."
        )
        return text, None, True

    display_link, connect_button_url = await prepare_config_links(settings, raw_config_link)
    return display_link, connect_button_url, False


async def is_paid_config_link_blocked(
    settings: Settings,
    session: AsyncSession,
    user_id: int,
) -> bool:
    if not settings.REQUIRED_CHANNEL_SUBSCRIBE_TO_USE:
        return False
    if not settings.REQUIRED_CHANNEL_ID:
        return False
    if user_id in settings.ADMIN_IDS:
        return False

    db_user = await user_dal.get_user_by_id(session, user_id)
    if not db_user:
        return True
    return not (
        db_user.channel_subscription_verified
        and db_user.channel_subscription_verified_for == settings.REQUIRED_CHANNEL_ID
    )
