"""JSON-LD builders. Text goes through render_jsonld() in the template, which
unicode-escapes < > & so nothing here can break out of the script tag."""
from __future__ import annotations

from ..config import settings


def organization_ld() -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": settings.app_name,
        "url": settings.base_url,
        "parentOrganization": {"@type": "Organization", "name": "Quick Drain", "url": settings.parent_site_url, "telephone": settings.phone_tel},
        "telephone": settings.phone_tel,
        "areaServed": "US",
    }


def product_ld(product: dict, reviews: list[dict]) -> dict:
    url = f"{settings.base_url}/products/{product['slug']}"
    offers = []
    for v in product.get("variants", []):
        offers.append({
            "@type": "Offer",
            "url": url,
            "sku": v["sku"],
            "name": v["name"],
            "price": f"{v['price_cents'] / 100:.2f}",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock" if v["stock"] > 0 else "https://schema.org/OutOfStock",
            "itemCondition": "https://schema.org/NewCondition",
            "shippingDetails": {
                "@type": "OfferShippingDetails",
                "shippingRate": {"@type": "MonetaryAmount", "value": f"{settings.flat_shipping_cents / 100:.2f}", "currency": "USD"},
                "shippingDestination": {"@type": "DefinedRegion", "addressCountry": "US"},
            },
        })
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product["name"],
        "description": product.get("tagline") or product.get("seo_description") or "",
        "sku": product["variants"][0]["sku"] if product.get("variants") else product["slug"],
        "brand": {"@type": "Brand", "name": "Quick Drain"},
        "url": url,
        "offers": offers,
    }
    hero = product.get("hero_image")
    if hero and hero.get("src"):
        data["image"] = [f"{settings.base_url}{hero['src']}"]
    if product.get("review_count"):
        data["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": product["rating_avg"], "reviewCount": product["review_count"], "bestRating": 5, "worstRating": 1}
        data["review"] = [
            {"@type": "Review", "author": {"@type": "Person", "name": r["author_name"]}, "reviewRating": {"@type": "Rating", "ratingValue": r["rating"], "bestRating": 5}, "reviewBody": r["body"], "datePublished": (r["created_at"] or "")[:10]}
            for r in reviews[:5]
        ]
    return data


def faq_ld(faqs: list[dict]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": f["question"], "acceptedAnswer": {"@type": "Answer", "text": f["answer"]}} for f in faqs],
    }


def breadcrumb_ld(items: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": name, "item": f"{settings.base_url}{path}"} for i, (name, path) in enumerate(items)],
    }


def article_ld(post: dict) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post["title"],
        "description": post.get("excerpt", ""),
        "datePublished": (post.get("published_at") or "")[:10],
        "dateModified": (post.get("updated_at") or "")[:10],
        "author": {"@type": "Organization", "name": post.get("author") or "Quick Drain"},
        "publisher": {"@type": "Organization", "name": settings.app_name, "url": settings.base_url},
        "mainEntityOfPage": f"{settings.base_url}/blog/{post['slug']}",
    }
