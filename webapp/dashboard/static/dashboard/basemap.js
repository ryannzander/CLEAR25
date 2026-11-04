/* ============================================================
   PM2.5 EWS — Dark basemap shared by the live map and /plan/
   ============================================================
   OpenFreeMap "dark" vector style, drawn through MapLibre GL + the
   maplibre-gl-leaflet binding. No API key, no registration, no request
   limits (https://openfreemap.org/). Replaced CARTO dark_all, which now
   watermarks every keyless tile with "API KEY REQUIRED".
   The layer lives in Leaflet's tilePane, so markers stay on top. */

var BASEMAP_STYLE_URL = "https://tiles.openfreemap.org/styles/dark";
var BASEMAP_ATTRIBUTION =
    '<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> ' +
    '&copy; <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> ' +
    'Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';

// Place labels are orientation, not data: dim them so they never compete
// with the station/sensor dots drawn on top.
var BASEMAP_LABEL_OPACITY = 0.5;

function dimBasemapLabels(glMap) {
    var layers = (glMap.getStyle() && glMap.getStyle().layers) || [];
    layers.forEach(function (layer) {
        if (layer.type !== "symbol") return;
        try {
            glMap.setPaintProperty(layer.id, "text-opacity", BASEMAP_LABEL_OPACITY);
            glMap.setPaintProperty(layer.id, "icon-opacity", BASEMAP_LABEL_OPACITY);
        } catch (e) { /* a layer without text/icon paint: leave it */ }
    });
}

function addDarkBasemap(map, extraAttribution) {
    var attribution = BASEMAP_ATTRIBUTION + (extraAttribution ? " · " + extraAttribution : "");
    var layer = L.maplibreGL({
        style: BASEMAP_STYLE_URL,
        // The binding reads the Leaflet attribution from here (its style's
        // sources carry none), and keeps MapLibre's own control disabled.
        attributionControl: { customAttribution: attribution },
    }).addTo(map);
    var glMap = layer.getMaplibreMap && layer.getMaplibreMap();
    if (glMap) {
        if (glMap.isStyleLoaded()) dimBasemapLabels(glMap);
        else glMap.once("style.load", function () { dimBasemapLabels(glMap); });
    }
    return layer;
}
