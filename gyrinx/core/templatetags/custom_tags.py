import hashlib
import re

import qrcode
import qrcode.image.svg
from django import template
from django.conf import settings
from django.core.cache import cache
from django.template.context import RequestContext
from django.urls import resolve
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from gyrinx.content.models import ContentPageRef
from gyrinx.core import url

register = template.Library()


def is_active(context: RequestContext, name):
    """Check if the current view is active."""
    return name == resolve(context.request.path).view_name


@register.simple_tag(takes_context=True)
def active_view(context: RequestContext, name):
    return "active" if is_active(context, name) else ""


@register.simple_tag(takes_context=True)
def active_aria(context: RequestContext, name):
    return 'aria-current="page"' if is_active(context, name) else ""


@register.simple_tag(takes_context=True)
def active_query(context: RequestContext, key, value):
    return "active" if context["request"].GET.get(key, "") == value else ""


@register.simple_tag(takes_context=True)
def active_query_aria(context: RequestContext, key, value):
    return 'aria-current="page"' if active_query(context, key, value) else ""


@register.simple_tag(takes_context=True)
def flash(context: RequestContext, id):
    request = context["request"]
    return "flash-warn" if request.GET.get("flash") == str(id) else ""


@register.filter
def lookup(dictionary, key):
    if isinstance(dictionary, list):
        # TODO: This assumes the list is a namedtuple with a 'grouper' attribute. This is a bit of a hack.
        item = next((item for item in dictionary if item.grouper == key), None)
        return item.list if item else None
    return dictionary.get(key)


@register.simple_tag
def qt(request, **kwargs):
    updated = request.GET.copy()
    for k, v in kwargs.items():
        if v is not None:
            updated[k] = v
        else:
            updated.pop(k, 0)  # Remove or return 0 - aka, delete safely this key

    return updated.urlencode()


@register.simple_tag
def qt_nth(request, **kwargs):
    nth = kwargs.pop("nth")
    updated = request.GET.copy()
    for k, v in kwargs.items():
        current = updated.getlist(k)
        if nth < len(current):
            current[nth] = v
        else:
            current.append(v)
        updated.setlist(k, current)

    return updated.urlencode()


@register.simple_tag
def qt_rm_nth(request, **kwargs):
    nth = kwargs.pop("nth")
    updated = request.GET.copy()
    for k, v in kwargs.items():
        if str(v) != "1":
            continue
        current = updated.getlist(k)
        if nth < len(current):
            current.pop(nth)
            updated.setlist(k, current)

    return updated.urlencode()


@register.simple_tag
def qt_append(request, **kwargs):
    updated = request.GET.copy()
    for k, v in kwargs.items():
        current = updated.getlist(k)
        current.append(v)
        updated.setlist(k, current)

    return updated.urlencode()


@register.simple_tag
def qt_rm(request, *args):
    updated = request.GET.copy()
    for k in args:
        updated.pop(k, 0)

    return updated.urlencode()


@register.simple_tag
def qt_contains(request, key, value):
    value = str(value)
    return key in request.GET and value in request.GET.getlist(key, list)


@register.filter(name="min")
def fmin(value, arg):
    return min(int(value), int(arg))


@register.filter(name="max")
def fmax(value, arg):
    return max(int(value), int(arg))


def identity(value):
    return value


@register.filter
def to_str(value):
    """converts int to string"""
    return str(value)


@register.simple_tag
def ref(*args, category=None, value=None):
    """
    Render a reference to a rulebook page.

    This tag takes a list of arguments and returns a link to the most similar
    rulebook page. If no similar page is found, the original string is returned.

    This tag is cached, so it can be called multiple times with the same arguments
    without incurring a performance penalty. The references almost never change,
    so this should be very safe to do.
    """
    search_value = " ".join(args)
    if not value:
        value = search_value

    search_value_hash = hashlib.sha1(search_value.encode("utf-8")).hexdigest()
    cache_key = f"ref_{search_value_hash}"

    kwargs = {}
    if category:
        kwargs["category"] = category
        cat_hash = hashlib.sha1(category.encode("utf-8")).hexdigest()
        cache_key += f"_{cat_hash}"

    if cache.has_key(cache_key):
        return cache.get(cache_key)

    refs = ContentPageRef.find_similar(search_value, **kwargs)

    if not refs:
        cache.set(cache_key, value)
        return value

    ref_str = ", ".join(ref.bookref() for ref in refs)

    full_ref = format_html(
        '<span data-bs-toggle="tooltip" data-bs-title="{}" class="tooltipped">{}</span>',
        ref_str,
        value,
    )
    cache.set(cache_key, full_ref)
    return full_ref


@register.simple_tag
def qr_svg(value):
    code = qrcode.make(
        value, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=0
    ).to_string(encoding="unicode")

    code = re.sub(r'width="\d+mm"', "", code)
    code = re.sub(r'height="\d+mm"', "", code)
    code = re.sub(r"<svg ", '<svg width="100%" height="100%" ', code)

    return mark_safe(code)


@register.simple_tag(takes_context=True)
def fullurl(context: RequestContext, path):
    return url.fullurl(context["request"], path)


@register.simple_tag
def settings_value(name):
    return getattr(settings, name, "")


@register.simple_tag
def get_skill(skill_id):
    """Get a ContentSkill by its ID."""
    from gyrinx.content.models import ContentSkill

    try:
        return ContentSkill.objects.get(pk=skill_id)
    except ContentSkill.DoesNotExist:
        return None


@register.filter
def subtract(value, arg):
    """Subtract arg from value."""
    try:
        return int(value) - int(arg)
    except (ValueError, TypeError):
        return 0
