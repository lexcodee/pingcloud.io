"""PingCloud i18n Static Site Generator.

Reads index.html template + i18n JSON files and generates pre-rendered
HTML files for each language with all text baked into the source.

Output:
  web/static/index.en.html  — English (fully rendered)
  web/static/index.zh.html  — Chinese (fully rendered)

Each output file includes:
  - All data-i18n text replaced with translated content
  - All data-i18n-placeholder values replaced
  - All data-i18n-content values replaced
  - Correct <html lang="..."> attribute
  - hreflang link tags for all languages
  - Canonical URL link tag
"""

import json
import re
from pathlib import Path

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE = _STATIC_DIR / "index.html"
_I18N_DIR = _STATIC_DIR / "i18n"
_SITE_URL = "https://pingcloud.io"

# Language config: (file_suffix, lang_attr, json_file, url_path)
# file_suffix: used in output filename (index.{suffix}.html)
# lang_attr: used in <html lang="..."> for precise language classification
LANGUAGES = [
    ("en", "en", _I18N_DIR / "en.json", "/"),
    ("zh", "zh-CN", _I18N_DIR / "zh.json", "/zh/"),
]


def _load_translations(json_path: Path) -> dict:
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _render_html(template: str, lang: str, translations: dict, url_path: str) -> str:
    """Replace all i18n placeholders in the template with translated text."""
    html = template

    # 1. Set <html lang="...">
    html = re.sub(
        r'(<html[^>]*lang=)"[^"]*"',
        rf'\1"{lang}"',
        html,
    )

    # 2. Replace data-i18n element content
    # Match: data-i18n="KEY">...text content...</closing_tag>
    # Handles text content that may contain HTML entities (&amp; etc.)
    # but NOT nested child elements (which have their own data-i18n)
    TAGS_WITH_I18N = r'(?:h[1-6]|p|span|a|div|td|th|li|button|label|option|title|strong|em|b|i)'

    # Two-pass approach:
    # Pass 1: Handle simple text-only content (no child elements, no entities)
    # data-i18n="key">plain text</tag>
    pattern_simple = rf'(data-i18n="([^"]+)">)([^<]+?)(</{TAGS_WITH_I18N}>)'

    def replacer_simple(m):
        key = m.group(2)
        value = translations.get(key, m.group(3))
        return m.group(1) + value + m.group(4)

    html = re.sub(pattern_simple, replacer_simple, html)

    # Pass 2: Handle content with HTML entities (e.g., &mdash; &copy;)
    # data-i18n="key">text with &entity; more text</tag>
    # This pattern matches text that contains &...; entities but no < tags
    pattern_entities = rf'(data-i18n="([^"]+)">)((?:[^<]|&[a-zA-Z]+;|&#[0-9]+;)+?)(</{TAGS_WITH_I18N}>)'

    def replacer_entities(m):
        key = m.group(2)
        original = m.group(3)
        # Only replace if we haven't already replaced this one
        # (check if the content matches the translation for the current lang)
        value = translations.get(key, original)
        return m.group(1) + value + m.group(4)

    html = re.sub(pattern_entities, replacer_entities, html)

    # 3. Replace data-i18n-placeholder attribute values
    # Handle both attribute orders: data-i18n-placeholder before or after placeholder
    # Order 1: data-i18n-placeholder="key" ... placeholder="value"
    html = re.sub(
        r'(data-i18n-placeholder="([^"]+)"[^>]*?placeholder=")[^"]*(")',
        lambda m: m.group(1) + translations.get(m.group(2), "") + m.group(3),
        html,
    )
    # Order 2: placeholder="value" ... data-i18n-placeholder="key"
    html = re.sub(
        r'(placeholder=")[^"]*("[^>]*?data-i18n-placeholder="([^"]+)")',
        lambda m: m.group(1) + translations.get(m.group(3), "") + m.group(2),
        html,
    )

    # 4. Replace data-i18n-content attribute values (meta description)
    # Find tags with data-i18n-content and replace their content="" attribute
    def replace_i18n_content_attr(m):
        """Replace content="..." in a tag that has data-i18n-content="key"."""
        tag_before = m.group(1)   # everything before content="
        key = m.group(2)          # data-i18n-content key
        tag_after = m.group(3)    # everything after the closing " of content
        value = translations.get(key, "")
        return tag_before + value + tag_after

    # Order 1: data-i18n-content="key" appears before content="value"
    html = re.sub(
        r'(data-i18n-content="([^"]+)"[^>]*?content=")[^"]*(")',
        replace_i18n_content_attr,
        html,
    )
    # Order 2: content="value" appears before data-i18n-content="key"
    html = re.sub(
        r'(content=")[^"]*("[^>]*?data-i18n-content="([^"]+)")',
        lambda m: m.group(1) + translations.get(m.group(3), "") + m.group(2),
        html,
    )

    # 5. Replace <title> content (special case — may contain &amp; entities)
    title_match = re.search(r'<title[^>]*data-i18n="([^"]+)"[^>]*>', html)
    if title_match:
        key = title_match.group(1)
        value = translations.get(key, "")
        # Replace everything between <title ...> and </title>
        html = re.sub(
            r'(<title[^>]*data-i18n="[^"]*"[^>]*>).*?(</title>)',
            rf'\1{value}\2',
            html,
            count=1,
            flags=re.DOTALL,
        )

    # 6. Add hreflang and canonical tags
    # Remove any existing hreflang/canonical tags (from previous builds)
    html = re.sub(r'<link[^>]*rel="alternate"[^>]*hreflang="[^"]*"[^>]*/?\s*>\n?', '', html)
    html = re.sub(r'<link[^>]*rel="canonical"[^>]*/?\s*>\n?', '', html)
    # Remove any existing JSON-LD schema tags (from previous builds)
    html = re.sub(r'<script type="application/ld\+json">.*?</script>\n?', '', html, flags=re.DOTALL)

    # Build hreflang tags
    hreflang_tags = []
    for _, other_hreflang, _, other_path in LANGUAGES:
        hreflang_tags.append(
            f'<link rel="alternate" hreflang="{other_hreflang}" href="{_SITE_URL}{other_path}" />'
        )
    # Add x-default pointing to English (default)
    hreflang_tags.append(
        f'<link rel="alternate" hreflang="x-default" href="{_SITE_URL}/" />'
    )

    # Canonical URL
    canonical_tag = f'<link rel="canonical" href="{_SITE_URL}{url_path}" />'

    # 7. Open Graph + Twitter Card meta tags
    og_title = translations.get("nav.pageTitle", "")
    og_desc = translations.get("meta.description", "")
    og_image = f"{_SITE_URL}/static/images/social-cover.png"
    og_url = f"{_SITE_URL}{url_path}"

    og_tags = [
        f'<meta property="og:type" content="website"/>',
        f'<meta property="og:url" content="{og_url}"/>',
        f'<meta property="og:title" content="{og_title}"/>',
        f'<meta property="og:description" content="{og_desc}"/>',
        f'<meta property="og:image" content="{og_image}"/>',
        f'<meta property="og:locale" content="{lang}"/>',
        f'<meta name="twitter:card" content="summary_large_image"/>',
        f'<meta name="twitter:title" content="{og_title}"/>',
        f'<meta name="twitter:description" content="{og_desc}"/>',
        f'<meta name="twitter:image" content="{og_image}"/>',
    ]

    # 8. Add Schema.org structured data (JSON-LD)
    schema_tags = []

    # WebSite schema
    site_name = translations.get("hero.brand", "PingCloud.io")
    website_schema = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": site_name,
        "url": f"{_SITE_URL}{url_path}",
        "description": translations.get("meta.description", ""),
        "inLanguage": lang,
    }
    schema_tags.append(
        f'<script type="application/ld+json">\n{json.dumps(website_schema, ensure_ascii=False, indent=2)}\n</script>'
    )

    # FAQPage schema — build from FAQ translation keys
    faq_entities = []
    for i in range(1, 7):
        q_key = f"howItWorks.faq.q{i}"
        a_key = f"howItWorks.faq.a{i}"
        q_text = translations.get(q_key, "")
        a_text = translations.get(a_key, "")
        if q_text and a_text:
            faq_entities.append({
                "@type": "Question",
                "name": q_text,
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": a_text,
                },
            })
    if faq_entities:
        faq_schema = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": faq_entities,
        }
        schema_tags.append(
            f'<script type="application/ld+json">\n{json.dumps(faq_schema, ensure_ascii=False, indent=2)}\n</script>'
        )

    # Insert before </head>
    seo_tags = "\n".join(hreflang_tags) + "\n" + canonical_tag
    seo_tags += "\n" + "\n".join(og_tags)
    if schema_tags:
        seo_tags += "\n" + "\n".join(schema_tags)
    html = html.replace("</head>", f"{seo_tags}\n</head>")

    return html


def build():
    """Generate pre-rendered HTML files for all languages."""
    if not _TEMPLATE.exists():
        print(f"Error: Template not found: {_TEMPLATE}")
        return

    with open(_TEMPLATE, "r", encoding="utf-8") as f:
        template = f.read()

    for suffix, lang_attr, json_path, url_path in LANGUAGES:
        if not json_path.exists():
            print(f"Error: Translation file not found: {json_path}")
            continue

        translations = _load_translations(json_path)
        rendered = _render_html(template, lang_attr, translations, url_path)

        output_path = _STATIC_DIR / f"index.{suffix}.html"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)

        print(f"✓ Generated {output_path} ({len(rendered)} bytes)")

    print(f"\nDone. Generated {len(LANGUAGES)} language versions.")


if __name__ == "__main__":
    build()
