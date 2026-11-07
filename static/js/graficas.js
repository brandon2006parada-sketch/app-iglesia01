"use strict";

function crearGraficaAsistencia(elementId, etiquetas, valores) {
  const canvas = document.getElementById(elementId);
  if (!canvas || typeof Chart === "undefined") return;

  new Chart(canvas, {
    type: "bar",
    data: {
      labels: etiquetas,
      datasets: [{
        label: "Asistentes",
        data: valores,
        backgroundColor: "rgba(53, 105, 212, 0.78)",
        hoverBackgroundColor: "#2858bd",
        borderRadius: 7,
        maxBarThickness: 36,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { displayColors: false },
      },
      scales: {
        x: { grid: { display: false }, border: { display: false } },
        y: {
          beginAtZero: true,
          ticks: { precision: 0 },
          grid: { color: "#edf0f5" },
          border: { display: false },
        },
      },
    },
  });
}
