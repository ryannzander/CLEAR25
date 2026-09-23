from django.conf import settings


def basemap(request):
    """Expose the public CARTO basemap key to the map templates."""
    return {"CARTO_BASEMAPS_KEY": settings.CARTO_BASEMAPS_KEY}
