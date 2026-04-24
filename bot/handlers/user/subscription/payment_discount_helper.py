import logging
from typing import Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from db.dal import active_discount_dal


async def apply_discount_to_payment(
    session: AsyncSession,
    user_id: int,
    original_price: float,
    promo_code_service=None,
    payment_kind: str = "base_subscription",
    offer_value: Optional[float] = None,
    stars: bool = False,
) -> Tuple[float, Optional[float], Optional[int]]:
    """
    Apply active discount to payment if exists.

    Returns:
        (final_price, discount_amount, promo_code_id)
    """
    if not promo_code_service:
        return original_price, None, None

    active_discount = await active_discount_dal.get_active_discount(
        session,
        user_id,
    )
    if not active_discount:
        return original_price, None, None

    promo_model = await promo_code_service.get_user_active_discount(
        session,
        user_id,
        payment_kind=payment_kind,
    )
    if not promo_model:
        return original_price, None, None
    discount_percentage, _promo_code, max_discount_amount, combined_discount_scope = promo_model

    if offer_value is not None:
        offer_details = promo_code_service.calculate_discounted_offer_details(
            value=offer_value,
            payment_kind=payment_kind,
            discount_percentage=discount_percentage,
            max_discount_amount=max_discount_amount,
            combined_discount_scope=combined_discount_scope,
            stars=stars,
        )
        if offer_details and abs(float(offer_details["original_price"]) - float(original_price)) < 0.01:
            final_price = float(offer_details["final_price"])
            discount_amount = float(offer_details["discount_amount"])
        else:
            final_price, discount_amount = promo_code_service.calculate_discounted_price(
                original_price,
                discount_percentage,
                max_discount_amount=max_discount_amount,
            )
    else:
        final_price, discount_amount = promo_code_service.calculate_discounted_price(
            original_price,
            discount_percentage,
            max_discount_amount=max_discount_amount,
        )

    logging.info(
        f"Applying {discount_percentage}% discount to payment for user {user_id}: "
        f"{original_price} -> {final_price}"
    )

    return final_price, discount_amount, active_discount.promo_code_id
