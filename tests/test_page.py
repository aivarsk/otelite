import unittest
from pathlib import Path

from test_http import HttpTestBase

ROOT = Path(__file__).resolve().parent.parent


class TestPageContract(HttpTestBase, unittest.TestCase):
    # Each test guards a UI regression the user reported by hand. The anchors
    # are structural (the exact mechanism that fixed it), so a red test means
    # either a regression or a deliberate page change that must update these.

    def setUp(self):
        super().setUp()
        _, _, body = self.get("/")
        self.html = body.decode() if isinstance(body, bytes) else body

    def test_title_names_the_product(self):
        self.assertIn("<title>OTELite.io - the disposable garage edition of OTEL</title>", self.html)

    def test_panels_are_collapsible(self):
        # section headers toggle a collapsed state so the page can shrink
        self.assertIn("section.collapsed > :not(h2)", self.html)
        self.assertIn('h.parentElement.classList.toggle("collapsed")', self.html)
        # charts re-render on uncollapse and window resize, and the scatter has
        # a fixed pixel size so it can never scale its fonts like an image
        self.assertIn('window.addEventListener("resize"', self.html)
        self.assertIn('svg.setAttribute("width", W)', self.html)

    def test_charts_render_axes_via_uplot(self):
        # "metrics charts have no values on axis" - charts must go through
        # uPlot with the shared axis config, not hand-rolled bare SVG.
        self.assertIn("/static/uplot.js", self.html)
        self.assertIn("/static/uplot.css", self.html)
        self.assertIn("axes: PLOT_AXES", self.html)
        # axis ticks across all charts share one range-based formatter that
        # gains detail as the range narrows (date -> HH:MM -> HH:MM:SS -> .sss)
        self.assertIn("function timeLabel", self.html)
        self.assertIn(
            "values: (u, splits) => splits.map((s) => timeLabel(s * 1000, state.toMs - state.fromMs))", self.html
        )
        # fewer vertical time lines: min 100px between uPlot ticks, and the
        # manual date entry fields mirror the selected range
        self.assertIn("space: 100", self.html)
        self.assertIn("function syncRangeInputs", self.html)
        # grafana style range sharing: range lives in the URL, copy button
        # grabs a link that opens another tab at the same range
        self.assertIn("function syncUrl", self.html)
        self.assertIn("history.replaceState", self.html)
        self.assertIn("URLSearchParams(location.search)", self.html)
        self.assertIn('id="copy-range"', self.html)
        # bug reports go to the issue tracker, stated where bugs get found
        self.assertIn('href="https://github.com/aivarsk/otelite/issues">fan mail</a>', self.html)
        # trace list and log line timestamps both carry date, time and
        # milliseconds, matching chart precision at full zoom
        self.assertIn('toLocaleTimeString("en-GB", { hour12: false }) + "." + String(d.getMilliseconds())', self.html)
        self.assertIn("${fmtDay(l.time_ms)}", self.html)
        # chart x values keep fractional seconds; rounding to whole seconds
        # collapsed zoomed charts into a single vertical line
        self.assertNotIn("Math.round((data.from_ms", self.html)
        self.assertNotIn("Math.round(ts / 1000)", self.html)

    def test_uplot_assets_served(self):
        status, content_type, body = self.get("/static/uplot.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", content_type)
        self.assertGreater(len(body), 40000)
        status, content_type, _ = self.get("/static/uplot.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)

    def test_time_picker_is_24h_text(self):
        # "make the time picker 24h" - datetime-local renders in browser
        # locale, so the picker must be text fields parsed as 24h.
        self.assertIn('placeholder="YYYY-MM-DD HH:mm"', self.html)
        self.assertIn("function parseDate", self.html)
        self.assertIn("hour12: false", self.html)
        self.assertNotIn("datetime-local", self.html)

    def test_log_trace_opens_inline(self):
        # "open the trace under the logline" - the waterfall must render in a
        # holder inserted after the clicked row, not only in the traces panel.
        self.assertIn("async function showTrace(id, container)", self.html)
        self.assertIn('detail.className = "trace-detail"', self.html)
        self.assertIn("row.after(detail)", self.html)
        self.assertIn('detail.querySelector(".inline-trace")', self.html)
        # the whole log line is clickable, not only the trace id link
        self.assertIn("row.onclick = () => toggleTraceRow(row, row.dataset.trace)", self.html)
        self.assertIn("tr[data-trace] { cursor:pointer; }", self.html)
        # "clicking a trace opens it under the clicked row" - the trace table
        # expands with a detail row under the clicked one, not at the bottom.
        self.assertIn('colspan="7"', self.html)
        self.assertNotIn('id="waterfall"', self.html)
        # "timelines must align like a table" - rows are a grid with a
        # uniform name column, not flex sized per row.
        self.assertIn("display:grid", self.html)
        self.assertIn("grid-template-columns:minmax(140px,330px) 1fr 64px", self.html)

    def test_log_list_matches_trace_table_style(self):
        # logs render as the same table shape as traces, no separate
        # monospace row styling
        self.assertIn("<th>severity</th>", self.html)
        self.assertIn('class="logbody"', self.html)
        self.assertNotIn("logline", self.html)
        # trace id links have no href, so they need the pointer cursor by hand
        self.assertIn("a[data-trace] { cursor:pointer; }", self.html)

    def test_logs_and_metrics_charts_share_width_source(self):
        # "make logs and metrics charts the same width" - both must size from
        # their container's clientWidth with the charts area at full width.
        self.assertIn(".charts { width:100%; }", self.html)
        self.assertGreaterEqual(self.html.count("Math.max(host.clientWidth, 320)"), 2)

    def test_severity_chips_also_filter_log_lines(self):
        # "selecting error/debug should also filter the logs" - the chip
        # handler must re-render the line list through the severity filter.
        self.assertIn("function renderLogLines()", self.html)
        self.assertIn('state.sevHidden.has(l.severity || "UNSPECIFIED")', self.html)
        self.assertGreaterEqual(self.html.count("renderLogLines();"), 2)

    def test_trace_scatter_and_waterfall_time_grid(self):
        # traces get the duration-vs-time scatter (Jaeger/Tempo style, dot
        # click opens the trace) and waterfall lanes sit on an absolute
        # time grid instead of floating anonymously.
        self.assertIn('id="trace-scatter"', self.html)
        self.assertIn("function renderTraceScatter", self.html)
        self.assertIn("expandTraceRow", self.html)
        self.assertIn('class="wf-tick"', self.html)
        self.assertIn("background-size:20% 100%", self.html)
        # the scatter must support drag-to-select time range like the uPlot charts
        self.assertIn('svg.addEventListener("pointerdown"', self.html)
        self.assertIn('svg.addEventListener("pointerup"', self.html)

    def test_chart_legend_has_no_placeholder_values(self):
        # uPlot's live legend renders "time: --" and "<series>: --" whenever
        # the cursor is off the chart (the same bug microsre-saas had). The
        # legend must be static: series names, no live values.
        self.assertIn("legend: { live: false }", self.html)
        self.assertNotIn("legend: { show: true }", self.html)

    def test_metric_list_is_scrollable_and_filterable(self):
        # "accumulate 100 metrics" - the picker needs a search box and a
        # scrollable container, otherwise it shoves the charts off the page.
        self.assertIn('id="metric-filter"', self.html)
        self.assertIn("overflow-y:auto", self.html)


class TestLandingPage(unittest.TestCase):
    # docs/index.html is static; guard its copy and assets by reading the file.

    def setUp(self):
        self.html = (ROOT / "docs" / "index.html").read_text()

    def test_uses_rasterized_sticker(self):
        # font-free rendering of the logo
        self.assertIn('src="sticker.png"', self.html)

    def test_shows_the_viewer_screenshot(self):
        self.assertIn('src="screenshot.jpg"', self.html)

    def test_get_it_without_install_sh(self):
        # download the pyz straight from releases and run it, no installer
        self.assertIn("releases page", self.html)
        self.assertIn("python3 otelite-*.pyz", self.html)
        self.assertIn("otelite --demo", self.html)

    def test_protocol_support_stated(self):
        # the OTLP matrix is on the page so the first comment is answered
        self.assertIn("gRPC on 4317 is not supported", self.html)
        self.assertIn("Binary protobuf is later", self.html)

    def test_no_competitors_on_the_page(self):
        # comparisons live in private/hn-prepared-answers.md, not on the site
        self.assertNotIn("Jaeger", self.html)

    def test_metrics_stance_is_on_the_page(self):
        # metrics get the same old-sysadmin framing as logs: raw rows, SQL
        self.assertIn("SQL and grep are the query languages", self.html)

    def test_on_a_server_in_ci(self):
        # the copy covers running it remotely: env var, compose hostname, artifacts
        self.assertIn("On a server, in CI", self.html)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", self.html)
        self.assertIn("http://otelite:4318", self.html)

    def test_garage_icon_next_to_github(self):
        self.assertIn('title="no octocats in the garage"', self.html)

    def test_taglines(self):
        self.assertIn("The Fast and the Disposable.", self.html)
        self.assertIn("3,500+ requests per second. 20,000+ records. No fsync. Nobody cares in the garage.", self.html)
        self.assertIn("Eventual consistency", self.html)
        self.assertIn('href="https://github.com/aivarsk/otelite/issues">fan mail</a>', self.html)

    def test_brand_capitalization(self):
        self.assertNotIn("OpenTelemetry", self.html)


class TestRepoShape(unittest.TestCase):
    def test_container_files_ship(self):
        root = Path(__file__).resolve().parent.parent
        self.assertTrue((root / "Dockerfile").exists())
        self.assertTrue((root / "docker-compose.yml").exists())
        self.assertIn("private", (root / ".dockerignore").read_text())
        # ENTRYPOINT (not CMD) so `docker run image --demo` appends the flag
        dockerfile = (root / "Dockerfile").read_text()
        self.assertIn('ENTRYPOINT ["python3", "/opt/otelite.pyz"]', dockerfile)
        self.assertNotIn("CMD ", dockerfile)

    def test_compose_uses_the_published_image(self):
        # the sample runs the registry image, building only as a fallback
        compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
        self.assertIn("image: ghcr.io/aivarsk/otelite:latest", compose)

    def test_readme_has_the_pull_line(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
        self.assertIn("docker run --rm -p 4318:4318 ghcr.io/aivarsk/otelite:latest", readme)


if __name__ == "__main__":
    unittest.main()
