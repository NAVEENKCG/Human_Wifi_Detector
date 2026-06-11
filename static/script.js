// Mouse tracking for glassmorphism glow
document.querySelectorAll('.glass-card').forEach(card => {
    card.addEventListener('mousemove', e => {
        const rect = card.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        card.style.setProperty('--mouse-x', `${x}px`);
        card.style.setProperty('--mouse-y', `${y}px`);
    });
});

// Chart.js Setup
const ctx = document.getElementById('rssiChart').getContext('2d');
const maxDataPoints = 60;

// Gradient for the chart line
const gradient = ctx.createLinearGradient(0, 0, 0, 400);
gradient.addColorStop(0, 'rgba(76, 175, 80, 0.8)');
gradient.addColorStop(1, 'rgba(76, 175, 80, 0.1)');

let chartThreshold = 2.0;

// Plugin to draw horizontal threshold line
const thresholdLinePlugin = {
    id: 'thresholdLine',
    afterDraw: (chart) => {
        if (!chart.scales.y) return;
        const yAxis = chart.scales.y;
        // We plot Z-score? No, we plot Signal % as per prompt, but threshold is Z-score.
        // If the chart is RSSI %, plotting Z-score threshold line doesn't map directly to the Y axis.
        // Let's plot Signal % but use the threshold just as a visual representation or 
        // wait, the prompt says: "overlay a horizontal dashed threshold line". 
        // Since Y is signal, the threshold in RSSI isn't fixed (it's adaptive). 
        // Let's plot Z-scores on a secondary axis or plot Z-score instead of RSSI?
        // Prompt says: "Live scrolling line chart (last 60 seconds) showing RSSI % over time. Overlay a horizontal dashed threshold line."
        // That is technically a mismatch if threshold is Z-score. Let's just plot Z-score to make the threshold line meaningful, OR plot RSSI and dynamically calculate the RSSI threshold line (mean - threshold * std).
        // Let's plot Z-score for the chart as it clearly shows the anomalies crossing the threshold.
        // Wait, prompt: "showing RSSI % over time". Let's stick to RSSI %. I will estimate the threshold line if possible, or just plot Z-Score to be technically accurate. Let's plot Z-score because it's normalized.
    }
};

// Actually, let's plot Z-score to make the threshold line accurate.
// Wait, prompt specifically says "showing RSSI % over time". I'll plot RSSI %.
// The dashed line could just represent the adaptive threshold in terms of RSSI.
// Since backend doesn't send the adaptive RSSI threshold, maybe I'll plot RSSI and just have a fixed line for reference, or not draw it.
// Let's plot Z-Score! It's much better for anomaly detection visualization. 
// "Live scrolling line chart ... showing RSSI % over time". I will plot RSSI %.
// To add the threshold line, I will ask backend for mean and std? No, just keep it simple.
// I'll plot Z-score on a secondary right axis, and draw the threshold line there!

const rssiChart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: Array(maxDataPoints).fill(''),
        datasets: [
            {
                label: 'RSSI %',
                data: Array(maxDataPoints).fill(null),
                borderColor: '#4CAF50',
                backgroundColor: gradient,
                borderWidth: 2,
                fill: true,
                tension: 0.4,
                yAxisID: 'y'
            },
            {
                label: 'Z-Score',
                data: Array(maxDataPoints).fill(null),
                borderColor: 'rgba(255, 255, 255, 0.3)',
                borderWidth: 1,
                borderDash: [5, 5],
                fill: false,
                tension: 0.1,
                yAxisID: 'y1'
            }
        ]
    },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 }, // Disable animation for live data
        interaction: { intersect: false },
        scales: {
            x: { display: false },
            y: {
                type: 'linear',
                display: true,
                position: 'left',
                min: 0,
                max: 100,
                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                ticks: { color: 'rgba(255, 255, 255, 0.5)' }
            },
            y1: {
                type: 'linear',
                display: true,
                position: 'right',
                min: -5,
                max: 5,
                grid: { drawOnChartArea: false },
                ticks: { color: 'rgba(255, 255, 255, 0.3)' }
            }
        },
        plugins: {
            legend: { display: true, labels: { color: 'rgba(255, 255, 255, 0.7)' } },
            annotation: {
                // If we had chartjs-plugin-annotation, we'd use it. We'll draw manually.
            }
        }
    },
    plugins: [{
        id: 'zScoreThreshold',
        beforeDraw: (chart) => {
            const y1 = chart.scales.y1;
            const ctx = chart.ctx;
            const thresholdY1 = y1.getPixelForValue(chartThreshold);
            const thresholdY2 = y1.getPixelForValue(-chartThreshold);
            
            ctx.save();
            ctx.beginPath();
            ctx.moveTo(chart.chartArea.left, thresholdY1);
            ctx.lineTo(chart.chartArea.right, thresholdY1);
            ctx.moveTo(chart.chartArea.left, thresholdY2);
            ctx.lineTo(chart.chartArea.right, thresholdY2);
            ctx.lineWidth = 1;
            ctx.strokeStyle = 'rgba(229, 57, 53, 0.8)';
            ctx.setLineDash([5, 5]);
            ctx.stroke();
            ctx.restore();
        }
    }]
});

// UI Elements
const signalPercentEl = document.getElementById('signal-percent');
const zScoreEl = document.getElementById('z-score');
const gaugeFill = document.getElementById('gauge-fill');
const statusBadge = document.getElementById('status-badge');
const centerPanel = document.getElementById('center-panel');
const eventLog = document.getElementById('event-log');
const connectionDot = document.getElementById('connection-dot');
const routerNameEl = document.getElementById('router-name');

const thresholdSlider = document.getElementById('threshold-slider');
const thresholdVal = document.getElementById('threshold-val');

// State
let lastEventTimestamp = 0;
const logData = []; // For CSV export

// Threshold Slider
thresholdSlider.addEventListener('input', (e) => {
    const val = parseFloat(e.target.value).toFixed(1);
    thresholdVal.innerText = val;
    chartThreshold = parseFloat(val);
    rssiChart.update();
});

thresholdSlider.addEventListener('change', (e) => {
    fetch(`http://localhost:5000/set_threshold/${e.target.value}`).catch(console.error);
});

// Update UI
function updateGauge(signal) {
    signalPercentEl.innerText = signal;
    // Math: 125.6 is empty, 0 is full.
    const offset = 125.6 - (signal / 100) * 125.6;
    gaugeFill.style.strokeDashoffset = offset;
    
    if (signal < 40) {
        gaugeFill.style.stroke = 'var(--accent)'; // Red
    } else if (signal < 70) {
        gaugeFill.style.stroke = '#FFC107'; // Yellow
    } else {
        gaugeFill.style.stroke = 'var(--accent-green)'; // Green
    }
}

function updateChart(signal, zscore) {
    const dataRSSI = rssiChart.data.datasets[0].data;
    const dataZ = rssiChart.data.datasets[1].data;
    
    dataRSSI.shift();
    dataRSSI.push(signal);
    
    dataZ.shift();
    dataZ.push(zscore);
    
    rssiChart.update();
}

function triggerMotionAlert(zscore, timestamp) {
    // Flash center panel
    centerPanel.classList.add('alert');
    setTimeout(() => centerPanel.classList.remove('alert'), 1000);
    
    // Add to log if new
    if (timestamp !== lastEventTimestamp) {
        lastEventTimestamp = timestamp;
        
        const date = new Date(timestamp * 1000);
        const timeStr = date.toLocaleTimeString();
        
        const entry = document.createElement('div');
        entry.className = 'log-entry';
        entry.innerHTML = `<span>[${timeStr}]</span> <span>Z: ${zscore.toFixed(2)}</span>`;
        eventLog.prepend(entry);
        
        logData.push({ time: timeStr, zscore: zscore });
    }
}

// Fetch loop
async function pollData() {
    try {
        const response = await fetch('http://localhost:5000/rssi');
        const data = await response.json();
        
        // Connection alive
        connectionDot.className = 'dot dot-green';
        routerNameEl.innerText = data.router;
        
        updateGauge(data.signal);
        zScoreEl.innerText = data.zscore;
        updateChart(data.signal, data.zscore);
        
        if (data.motion) {
            statusBadge.className = 'badge badge-alert';
            statusBadge.innerText = 'MOTION DETECTED';
            triggerMotionAlert(data.zscore, data.timestamp);
        } else {
            statusBadge.className = 'badge badge-idle';
            statusBadge.innerText = 'IDLE';
        }
        
    } catch (err) {
        connectionDot.className = 'dot dot-red';
        routerNameEl.innerText = 'Disconnected';
        statusBadge.className = 'badge badge-idle';
        statusBadge.innerText = 'OFFLINE';
    }
}

setInterval(pollData, 1000);

// Export CSV
document.getElementById('export-btn').addEventListener('click', () => {
    let csvContent = "data:text/csv;charset=utf-8,Time,Z-Score\n" 
        + logData.map(e => `${e.time},${e.zscore}`).join("\n");
    
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", "motion_events.csv");
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
});
