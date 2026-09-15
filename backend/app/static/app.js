/* sidequest frontend: Leaflet map + Chart.js elevation profile. */
(function () {
  "use strict";

  var currentMap = null;
  var currentChart = null;

  var CATEGORY_COLORS = {
    viewpoint: "#0ea5e9",
    peak: "#7c3aed",
    lake: "#2563eb",
    historic: "#b45309",
    mountain_pass: "#dc2626",
    mtb_trail: "#16a34a",
    mtb_route: "#16a34a",
    attraction: "#db2777",
    picnic_site: "#65a30d",
    park: "#059669",
    track: "#ca8a04",
    poi: "#64748b",
  };

  function colorFor(category) {
    return CATEGORY_COLORS[category] || CATEGORY_COLORS.poi;
  }

  function haversineKm(a, b) {
    var toRad = Math.PI / 180;
    var R = 6371.0088;
    var dLat = (b[0] - a[0]) * toRad;
    var dLon = (b[1] - a[1]) * toRad;
    var lat1 = a[0] * toRad;
    var lat2 = b[0] * toRad;
    var h =
      Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  function downsample(geometry, elevations, maxPoints) {
    var count = geometry.length;
    if (count <= maxPoints) {
      return { geometry: geometry, elevations: elevations };
    }
    var step = count / maxPoints;
    var outGeo = [];
    var outEle = [];
    for (var i = 0; i < maxPoints; i++) {
      var idx = Math.floor(i * step);
      outGeo.push(geometry[idx]);
      outEle.push(elevations[idx] || 0);
    }
    outGeo.push(geometry[count - 1]);
    outEle.push(elevations[count - 1] || 0);
    return { geometry: outGeo, elevations: outEle };
  }

  function renderMap(plan) {
    var el = document.getElementById("route-map");
    if (!el) return;
    if (currentMap) {
      currentMap.remove();
      currentMap = null;
    }

    var coords = plan.geometry.map(function (pt) {
      return [pt[0], pt[1]];
    });
    if (!coords.length) return;

    var map = L.map(el);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);

    var line = L.polyline(coords, { color: "#059669", weight: 4, opacity: 0.85 }).addTo(map);

    L.circleMarker(coords[0], {
      radius: 7,
      color: "#065f46",
      fillColor: "#10b981",
      fillOpacity: 1,
    })
      .addTo(map)
      .bindPopup("Start");

    if (!plan.round_trip) {
      L.circleMarker(coords[coords.length - 1], {
        radius: 7,
        color: "#7f1d1d",
        fillColor: "#ef4444",
        fillOpacity: 1,
      })
        .addTo(map)
        .bindPopup("Ziel");
    }

    (plan.highlights || []).forEach(function (highlight, index) {
      L.circleMarker([highlight.lat, highlight.lon], {
        radius: 8,
        color: "#ffffff",
        weight: 2,
        fillColor: colorFor(highlight.category),
        fillOpacity: 1,
      })
        .addTo(map)
        .bindPopup(
          "<strong>" +
            (index + 1) +
            ". " +
            highlight.name +
            "</strong><br>" +
            (highlight.rationale || highlight.category)
        );
    });

    map.fitBounds(line.getBounds(), { padding: [24, 24] });
    currentMap = map;
  }

  function renderChart(plan) {
    var canvas = document.getElementById("elevation-chart");
    if (!canvas || typeof Chart === "undefined") return;
    if (currentChart) {
      currentChart.destroy();
      currentChart = null;
    }

    var sampled = downsample(plan.geometry, plan.elevations, 400);
    var labels = [];
    var data = [];
    var cumulative = 0;
    for (var i = 0; i < sampled.geometry.length; i++) {
      if (i > 0) {
        cumulative += haversineKm(sampled.geometry[i - 1], sampled.geometry[i]);
      }
      labels.push(cumulative.toFixed(1));
      data.push(sampled.elevations[i]);
    }

    currentChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Höhe (m)",
            data: data,
            borderColor: "#059669",
            backgroundColor: "rgba(5, 150, 105, 0.15)",
            borderWidth: 2,
            pointRadius: 0,
            fill: true,
            tension: 0.25,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            title: { display: true, text: "km" },
            ticks: { maxTicksLimit: 8 },
            grid: { display: false },
          },
          y: {
            title: { display: true, text: "m" },
            grid: { color: "rgba(148, 163, 184, 0.2)" },
          },
        },
        plugins: { legend: { display: false } },
      },
    });
  }

  function renderPlan(plan) {
    if (!plan) return;
    renderMap(plan);
    renderChart(plan);
  }

  document.body.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    var holder = target ? target.querySelector("#plan-data") : null;
    if (!holder) return;
    try {
      renderPlan(JSON.parse(holder.textContent));
    } catch (error) {
      console.error("Could not render plan", error);
    }
  });
})();
