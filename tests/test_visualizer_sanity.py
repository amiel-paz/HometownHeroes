from __future__ import annotations

import gzip
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VISUALIZER_PATH = ROOT / "scratch" / "pilot_visualizer_experiment.py"
BIRTHPLACE_PIPELINE_PATH = ROOT / "pipelines" / "enrich_wikidata_birthplace.py"
MEDIA_PIPELINE_PATH = ROOT / "pipelines" / "enrich_player_media.py"


def load_visualizer():
    spec = importlib.util.spec_from_file_location("pilot_visualizer_experiment", VISUALIZER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


viz = load_visualizer()


def load_birthplace_pipeline():
    spec = importlib.util.spec_from_file_location("enrich_wikidata_birthplace", BIRTHPLACE_PIPELINE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


birthplace_pipeline = load_birthplace_pipeline()


def load_media_pipeline():
    spec = importlib.util.spec_from_file_location("enrich_player_media", MEDIA_PIPELINE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


media_pipeline = load_media_pipeline()


class VisualizerTaxonomyTests(unittest.TestCase):
    def test_college_layers_are_merged_and_school_is_hidden(self) -> None:
        self.assertNotIn("attended_school", viz.EVENT_TYPE_GROUPS.get("college", []))
        self.assertEqual(viz.EVENT_LAYER_BY_TYPE["attended_college"], "college")
        self.assertEqual(viz.EVENT_LAYER_BY_TYPE["played_college"], "college")
        self.assertEqual(viz.EVENT_LABELS["college"], "College")
        self.assertNotIn("School", set(viz.EVENT_LABELS.values()))
        self.assertNotIn("College Sports", set(viz.EVENT_LABELS.values()))
        self.assertNotIn("school", viz.TIMELINE_SECTION_ORDER)
        self.assertIsNone(viz.timeline_section_for_event("attended_school"))

    def test_or_groups_and_and_clauses_normalize(self) -> None:
        query = viz.normalize_query(
            {
                "lat": 37.3382,
                "lon": -121.8863,
                "groups": [
                    {"clauses": [{"kind": "birthplace"}, {"kind": "high_school"}]},
                    {"clauses": [{"kind": "college"}]},
                ],
            }
        )
        self.assertEqual(
            query["groups"],
            [
                {"clauses": [{"kind": "birthplace"}, {"kind": "high_school"}]},
                {"clauses": [{"kind": "college"}]},
            ],
        )
        self.assertEqual(
            viz.event_types_for_query(query),
            ["attended_college", "attended_high_school", "born", "played_college"],
        )

    def test_mobile_resize_does_not_always_collapse_expanded_filters(self) -> None:
        self.assertIn("let wasCompactLayout = isCompactLayout();", viz.HTML)
        self.assertIn("if (compact === wasCompactLayout)", viz.HTML)
        self.assertIn("runQuery({ collapseCompact: true })", viz.HTML)
        self.assertIn("options.collapseCompact === true", viz.HTML)

    def test_public_page_has_portfolio_metadata_and_media_fallbacks(self) -> None:
        self.assertIn("HometownHeroes Geospatial Sports Analytics", viz.HTML)
        self.assertIn('name="description"', viz.HTML)
        self.assertIn('rel="icon"', viz.HTML)
        self.assertIn("function wireAvatarFallback(root)", viz.HTML)
        self.assertIn("data-initials", viz.HTML)
        self.assertIn("No players match this query.", viz.HTML)
        self.assertIn("overflow-wrap: anywhere;", viz.HTML)

    def test_nfl_pfr_variants_include_uppercase_directory_prefix(self) -> None:
        self.assertEqual(birthplace_pipeline.pfr_variants("andermor01"), ["A/andermor01", "a/andermor01"])
        self.assertEqual(birthplace_pipeline.pfr_variants("a/andermor01"), ["A/andermor01", "a/andermor01"])

    def test_nfl_pastteam_parser_strips_wikilink_parentheses_before_years(self) -> None:
        line = "* [[Las Vegas Outlaws (XFL)|Las Vegas Outlaws]] (2001)"
        self.assertEqual(viz.nfl_pastteam_label_from_line(line), "Las Vegas Outlaws")
        self.assertEqual(viz.extract_nfl_years(line), "2001")

    def test_nhl_media_query_uses_nhlcom_wikidata_property(self) -> None:
        query = media_pipeline.wikidata_media_query(
            [
                {
                    "sport": "NHL",
                    "source_player_id": "8447400",
                    "wikidata_qid": "",
                    "external_property": "P3522",
                    "external_id": "8447400",
                }
            ]
        )
        self.assertIn("wdt:P3522", query)
        self.assertIn("?nhl", query)


class AttributionTests(unittest.TestCase):
    def test_public_attributions_are_linked_and_named(self) -> None:
        self.assertIn("/attributions", viz.HTML)
        text = viz.ATTRIBUTIONS_PATH.read_text(encoding="utf-8")
        for name in (
            "OpenStreetMap",
            "Leaflet",
            "GeoNames",
            "Wikidata",
            "Wikipedia",
            "Wikimedia Commons",
            "nflverse",
            "Lahman",
            "hoopR",
            "NHL public records",
        ):
            self.assertIn(name, text)

    def test_text_page_escapes_content(self) -> None:
        page = viz.render_text_page("A < B", "credit: A & B < C").decode("utf-8")
        self.assertIn("A &lt; B", page)
        self.assertIn("A &amp; B &lt; C", page)

    def test_attribution_page_renders_markdown_structure(self) -> None:
        page = viz.render_text_page(
            "Attributions",
            "# Attributions\n\nText with `LICENSE`.\n\n## Map and UI\n\n- Leaflet: https://leafletjs.com/ ; BSD.",
        ).decode("utf-8")
        self.assertIn("<h1>Attributions</h1>", page)
        self.assertIn("<h2>Map and UI</h2>", page)
        self.assertIn("<code>LICENSE</code>", page)
        self.assertIn("<ul><li>Leaflet:", page)
        self.assertIn('<a href="https://leafletjs.com/"', page)
        self.assertNotIn("# Attributions", page)
        self.assertNotIn("- Leaflet", page)


class RenderDeployTests(unittest.TestCase):
    def test_render_blueprint_declares_python_web_service(self) -> None:
        text = (ROOT / "render.yaml").read_text(encoding="utf-8")
        for snippet in (
            "type: web",
            "runtime: python",
            "python pipelines/render_build.py",
            "--host 0.0.0.0 --port $PORT",
            "healthCheckPath: /api/status",
            "HH_DB_ARTIFACT_URL",
            "HH_DB_ARTIFACT_REPO",
            "HH_DB_ARTIFACT_RELEASE_TAG",
            "HH_DB_ARTIFACT_ASSET_NAME",
            "HH_DB_ARTIFACT_TOKEN",
            "HH_DB_ARTIFACT_SHA256",
        ):
            self.assertIn(snippet, text)
        build_script = (ROOT / "pipelines" / "render_build.py").read_text(encoding="utf-8")
        self.assertIn("hydrate_app_database_from_url()", build_script)
        self.assertIn("resolve_app_database_artifact_url()", build_script)
        self.assertIn("HH_DB_ARTIFACT_URL", build_script)
        self.assertIn("HH_DB_ARTIFACT_RELEASE_TAG", build_script)
        self.assertIn("HH_DB_ARTIFACT_ASSET_NAME", build_script)
        self.assertIn("HH_DB_ARTIFACT_TOKEN", build_script)
        self.assertIn("HH_DB_ARTIFACT_SHA256", build_script)
        self.assertIn("pipelines/enrich_nba_alltime_wikidata.py", build_script)
        self.assertIn("pipelines/ingest_nhl.py", build_script)
        self.assertIn("HH_RENDER_INCLUDE_MEDIA", build_script)
        self.assertIn("MLB,NFL,NBA,NHL", build_script)
        self.assertIn("pipelines/build_pro_venue_stints.py", build_script)
        self.assertIn("data\" / \"derived", build_script)
        self.assertIn("player_media.sqlite.gz", build_script)
        self.assertIn("birthplace_audit_fixes.sqlite.gz", build_script)
        self.assertIn("hydrate_birthplace_audit_fixes_from_artifact()", build_script)

    def test_repo_path_handles_hosted_database_paths(self) -> None:
        outside = Path("/tmp/HometownHeroes.sqlite")
        rendered = viz.repo_path(outside)
        self.assertTrue(rendered.startswith("/"))
        self.assertTrue(rendered.endswith("HometownHeroes.sqlite"))

    def test_derived_media_artifact_is_readable(self) -> None:
        artifact = ROOT / "data" / "derived" / "player_media.sqlite.gz"
        self.assertTrue(artifact.exists())
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "player_media.sqlite"
            with gzip.open(artifact, "rb") as source, db_path.open("wb") as target:
                shutil.copyfileobj(source, target)
            con = sqlite3.connect(db_path)
            try:
                usable = con.execute(
                    "select count(*) from player_media where usable=1 and thumbnail_url is not null and thumbnail_url != ''"
                ).fetchone()[0]
            finally:
                con.close()
        self.assertGreater(usable, 1000)

    def test_derived_birthplace_audit_fixes_artifact_is_readable(self) -> None:
        artifact = ROOT / "data" / "derived" / "birthplace_audit_fixes.sqlite.gz"
        self.assertTrue(artifact.exists())
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "birthplace_audit_fixes.sqlite"
            with gzip.open(artifact, "rb") as source, db_path.open("wb") as target:
                shutil.copyfileobj(source, target)
            con = sqlite3.connect(db_path)
            try:
                birthdate_overrides = con.execute("select count(*) from player_birthdate_overrides").fetchone()[0]
                birthplace_overrides = con.execute("select count(*) from birthplace_overrides").fetchone()[0]
            finally:
                con.close()
        self.assertGreater(birthdate_overrides, 20000)
        self.assertGreaterEqual(birthplace_overrides, 0)


class RecruiterFramingTests(unittest.TestCase):
    def test_readme_frames_deployable_analytical_data_product(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for snippet in (
            "Live Demo",
            "Project Summary",
            "Technical Highlights",
            "Data Pipeline",
            "SQLite Warehouse",
            "Geospatial Analytics / BI Layer",
            "Data Quality and Attribution",
            "Deployment",
            "Limitations",
            "Local Development",
            "raw source snapshots",
            "unified SQLite warehouse",
            "map/query visualizer",
            "82,434 athletes",
            "python3 -m unittest discover -s tests",
        ):
            self.assertIn(snippet, text)


class BirthplaceAuditTests(unittest.TestCase):
    def test_birthplace_audit_and_fixer_scripts_are_documented(self) -> None:
        readme = (ROOT / "pipelines" / "README.md").read_text(encoding="utf-8")
        for snippet in (
            "pipelines/audit_birthplace_coverage.py",
            "scratch/birthplace_coverage_audit.sqlite",
            "scratch/birthplace_conflict_review_candidates.json",
            "pipelines/apply_birthplace_audit_fixes.py",
            "pipelines/audit_pro_year_coverage.py",
            "pipelines/ingest_nhl.py",
            "pipelines/package_release_artifacts.py",
            "pipelines/build_pro_venue_stints.py",
            "pipelines/run_nhl_media.sh",
            "scratch/pro_year_coverage_audit.sqlite",
            "scratch/pro_venue_stints.sqlite",
            "data/curation/pro_venue_stints.json",
            "data/curation/birthplace_overrides.json",
            "data/derived/birthplace_audit_fixes.sqlite.gz",
            "--include-complete-birthplaces",
        ):
            self.assertIn(snippet, readme)

    def test_curated_birthplace_overrides_include_mac_jones_conflict_fix(self) -> None:
        path = ROOT / "data" / "curation" / "birthplace_overrides.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        mac_rows = [row for row in rows if row["sport"] == "NFL" and row["player_id"] == "00-0036972"]
        self.assertEqual(len(mac_rows), 1)
        self.assertEqual(mac_rows[0]["label"], "Jacksonville, FL")
        self.assertEqual(mac_rows[0]["event_type"], "born")
        self.assertIn("Wikidata conflict override", mac_rows[0]["source"])


class RepositoryScrubTests(unittest.TestCase):
    def test_tracked_project_text_has_no_local_identity_fingerprints(self) -> None:
        files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
        suffixes = {".css", ".html", ".js", ".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
        forbidden = ("/" + "Users/", "martinez" + "lab", "Hg" + "Git", "Hometown" + "_Heroes")
        hits: list[str] = []
        for relative in filter(None, files):
            path = ROOT / relative
            if relative.startswith("data/raw/") or path.suffix not in suffixes:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for needle in forbidden:
                if needle in text:
                    hits.append(f"{relative}: {needle}")
        self.assertEqual([], hits)


class OptionalDatabaseTests(unittest.TestCase):
    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_nhl_is_available_in_query_and_finds_wayne_gretzky_birthplace(self) -> None:
        normalized = viz.normalize_query({"sports": ["NHL"]})
        self.assertEqual(normalized["sports"], ["NHL"])
        self.assertIn('id="sportNHL"', viz.HTML)

        con = viz.connect()
        try:
            nhl_players = con.execute("select count(*) from players where sport = 'NHL'").fetchone()[0]
            gretzky = con.execute(
                """
                select p.player_id, p.display_name, p.debut_year, p.final_year, l.label, l.country
                from players p
                join geocoded_player_location_events e
                  on e.sport = p.sport and e.player_id = p.player_id and e.event_type = 'born'
                join locations l on l.location_id = e.location_id
                where p.sport = 'NHL' and p.display_name = 'Wayne Gretzky'
                """
            ).fetchone()
            nhl_pro = con.execute(
                "select count(*) from pro_career_summary where sport = 'NHL'"
            ).fetchone()[0]
        finally:
            con.close()

        self.assertGreaterEqual(nhl_players, 23000)
        self.assertGreaterEqual(nhl_pro, 7000)
        self.assertIsNotNone(gretzky)
        self.assertEqual(gretzky["debut_year"], 1979)
        self.assertEqual(gretzky["final_year"], 1998)
        self.assertEqual(gretzky["label"], "Brantford, ON")
        self.assertIn(gretzky["country"], {"CAN", "Canada"})

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_nhl_media_enrichment_has_reusable_thumbnails(self) -> None:
        con = viz.connect()
        try:
            usable = con.execute(
                """
                select count(*)
                from player_media
                where sport = 'NHL'
                  and usable = 1
                  and thumbnail_url is not null
                  and thumbnail_url != ''
                """
            ).fetchone()[0]
            gretzky = con.execute(
                """
                select thumbnail_url, attribution_text, license
                from player_media
                where sport = 'NHL'
                  and player_id = '8447400'
                  and usable = 1
                """
            ).fetchone()
        finally:
            con.close()
        self.assertGreaterEqual(usable, 6000)
        self.assertIsNotNone(gretzky)
        self.assertTrue(gretzky["thumbnail_url"].startswith("https://upload.wikimedia.org/"))
        self.assertTrue(gretzky["license"])

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_mlb_media_joins_chadwick_bbref_ids_to_lahman_players(self) -> None:
        con = viz.connect()
        try:
            cc = con.execute(
                """
                select p.player_id, p.primary_external_id, pm.thumbnail_url, pm.file_title, pm.license
                from players p
                left join player_media pm
                  on pm.sport = p.sport and pm.player_id = p.player_id and pm.usable = 1
                where p.sport = 'MLB'
                  and p.player_id = 'sabatcc01'
                """
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(cc)
        self.assertEqual(cc["primary_external_id"], "sabatc.01")
        self.assertEqual(cc["file_title"], "File:CCSabathia.jpg")
        self.assertTrue(cc["thumbnail_url"].startswith("https://upload.wikimedia.org/"))
        self.assertTrue(cc["license"])

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_san_jose_college_query_uses_single_college_layer(self) -> None:
        query = viz.normalize_query(
            {
                "place": "San Jose, CA",
                "lat": 37.3382,
                "lon": -121.8863,
                "radius_mi": 50,
                "pro_start_year": 1970,
                "pro_end_year": 2026,
                "birth_start_year": 1800,
                "birth_end_year": 2026,
                "sports": ["MLB", "NFL", "NBA"],
                "groups": [{"clauses": [{"kind": "college"}]}],
            }
        )
        response = viz.build_response(query)
        event_labels = {feature["properties"]["event_label"] for feature in response["locations_geojson"]["features"]}
        event_types = {feature["properties"]["event_type"] for feature in response["locations_geojson"]["features"]}
        self.assertLessEqual(event_labels, {"College"})
        self.assertLessEqual(event_types, {"college"})

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_puerto_rico_birthplace_coverage_includes_lahman_pr(self) -> None:
        con = viz.connect()
        try:
            mlb_born, total = con.execute(
                """
                with pr_locations as (
                    select location_id
                    from locations
                    where upper(coalesce(country, '')) in ('PR', 'P.R.', 'PRI')
                       or upper(coalesce(state, '')) in ('PR', 'P.R.', 'PUERTO RICO')
                       or lower(label) like '%puerto rico%'
                )
                select
                    count(distinct case when sport = 'MLB' and event_type = 'born' then sport || ':' || player_id end),
                    count(distinct sport || ':' || player_id)
                from geocoded_player_location_events
                join pr_locations using (location_id);
                """
            ).fetchone()
        finally:
            con.close()
        self.assertGreaterEqual(mlb_born, 311)
        self.assertGreaterEqual(total, 328)

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_mlb_us_birthplace_fallbacks_cover_common_aliases(self) -> None:
        expected_minimums = {
            ("Brooklyn", "NY"): 250,
            ("Bronx", "NY"): 30,
            ("Queens", "NY"): 10,
            ("Van Nuys", "CA"): 15,
            ("Roxbury", "MA"): 10,
        }
        con = viz.connect()
        try:
            rows = con.execute(
                """
                select city, state, count(distinct player_id) as players
                from geocoded_player_location_events
                where sport = 'MLB'
                  and event_type = 'born'
                  and (city, state) in (
                    ('Brooklyn', 'NY'),
                    ('Bronx', 'NY'),
                    ('Queens', 'NY'),
                    ('Van Nuys', 'CA'),
                    ('Roxbury', 'MA')
                  )
                group by city, state;
                """
            ).fetchall()
        finally:
            con.close()
        observed = {(row["city"], row["state"]): row["players"] for row in rows}
        for key, minimum in expected_minimums.items():
            self.assertGreaterEqual(observed.get(key, 0), minimum, key)

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_nfl_profile_dates_prefer_canonical_career_years(self) -> None:
        con = viz.connect()
        try:
            player = con.execute(
                """
                select debut_year, final_year
                from players
                where sport = 'NFL' and player_id = '00-0003942'
                """
            ).fetchone()
            pro_locations = con.execute(
                """
                select pro_start_year, pro_end_year
                from pro_career_summary
                where sport = 'NFL' and player_id = '00-0003942'
                """
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(player)
        self.assertEqual(player["debut_year"], 1992)
        self.assertEqual(player["final_year"], 2000)
        self.assertIsNotNone(pro_locations)
        self.assertEqual(pro_locations["pro_start_year"], 1999)

        query = viz.normalize_query(
            {
                "place": "Kennewick, WA",
                "lat": 46.2112,
                "lon": -119.1372,
                "radius_mi": 20,
                "pro_start_year": 1970,
                "pro_end_year": 2026,
                "birth_start_year": 1800,
                "birth_end_year": 2026,
                "sports": ["NFL"],
                "groups": [{"clauses": [{"kind": "birthplace"}]}],
            }
        )
        response = viz.build_response(query)
        anthony_davis = [
            row for row in response["players"] if row["sport"] == "NFL" and row["player_id"] == "00-0003942"
        ]
        self.assertEqual(len(anthony_davis), 1)
        self.assertEqual(anthony_davis[0]["pro_career_length"], 9)
        timeline = {section["key"]: section["items"] for section in anthony_davis[0]["timeline"]}
        self.assertIn("birthplace", timeline)
        self.assertIn("high_school", timeline)
        self.assertIn("college", timeline)
        self.assertIn("pro", timeline)
        pro_labels = {item["label"] for item in timeline["pro"]}
        self.assertIn("Houston Oilers / Astrodome", pro_labels)
        self.assertIn("Seattle Seahawks / Kingdome", pro_labels)
        self.assertIn("Kansas City Chiefs / Arrowhead Stadium", pro_labels)
        self.assertIn("Green Bay Packers / Lambeau Field", pro_labels)
        self.assertIn("Baltimore Ravens / PSINet Stadium", pro_labels)
        self.assertNotIn("Houston Oilers", pro_labels)
        self.assertNotIn("Seattle Seahawks", pro_labels)
        self.assertNotIn("Kansas City Chiefs", pro_labels)
        self.assertNotIn("Green Bay Packers", pro_labels)
        self.assertNotIn("Baltimore Ravens", pro_labels)
        chiefs = [item for item in timeline["pro"] if item["label"] == "Kansas City Chiefs / Arrowhead Stadium"]
        self.assertEqual(chiefs[0]["years"], "1994-1998")

    def test_expanded_timeline_collapses_common_alias_duplicates(self):
        query = viz.normalize_query(
            {
                "place": "San Jose, CA",
                "lat": 37.3382,
                "lon": -121.8863,
                "radius_mi": 50,
                "pro_start_year": 1970,
                "pro_end_year": 2026,
                "birth_start_year": 1800,
                "birth_end_year": 2026,
                "sports": ["NFL"],
                "groups": [{"clauses": [{"kind": "birthplace"}]}, {"clauses": [{"kind": "high_school"}]}],
            }
        )
        response = viz.build_response(query)
        courtney_bryan = [
            row for row in response["players"] if row["sport"] == "NFL" and row["display_name"] == "Courtney Bryan"
        ]
        self.assertEqual(len(courtney_bryan), 1)
        timeline = {section["key"]: section["items"] for section in courtney_bryan[0]["timeline"]}
        college_labels = [item["label"] for item in timeline["college"]]
        pro_labels = [item["label"] for item in timeline["pro"]]
        self.assertEqual(college_labels.count("New Mexico State University"), 1)
        self.assertNotIn("New Mexico State University-Main Campus", college_labels)
        self.assertIn("Miami Dolphins / Dolphin Stadium", pro_labels)
        self.assertNotIn("MIA / Dolphin Stadium", pro_labels)

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_sombor_serbia_birthplace_query_finds_nikola_jokic(self) -> None:
        con = viz.connect()
        try:
            serbia_nba_birthplaces = con.execute(
                """
                select count(distinct e.player_id)
                from geocoded_player_location_events e
                join locations l using (location_id)
                where e.sport = 'NBA'
                  and e.event_type = 'born'
                  and l.country = 'Serbia'
                """
            ).fetchone()[0]
            jokic_birthplace = con.execute(
                """
                select l.label, l.country, l.latitude, l.longitude
                from geocoded_player_location_events e
                join locations l using (location_id)
                where e.sport = 'NBA'
                  and e.player_id = '3112335'
                  and e.event_type = 'born'
                """
            ).fetchone()
        finally:
            con.close()

        self.assertGreaterEqual(serbia_nba_birthplaces, 30)
        self.assertIsNotNone(jokic_birthplace)
        self.assertEqual(jokic_birthplace["label"], "Sombor, Sombor City")
        self.assertEqual(jokic_birthplace["country"], "Serbia")

        query = viz.normalize_query(
            {
                "place": "Sombor, Serbia",
                "lat": 45.78,
                "lon": 19.12,
                "radius_mi": 20,
                "pro_start_year": 1970,
                "pro_end_year": 2026,
                "birth_start_year": 1800,
                "birth_end_year": 2026,
                "sports": ["NBA"],
                "groups": [{"clauses": [{"kind": "birthplace"}]}],
            }
        )
        response = viz.build_response(query)
        jokic = [row for row in response["players"] if row["sport"] == "NBA" and row["player_id"] == "3112335"]
        self.assertEqual(len(jokic), 1)
        timeline = {section["key"]: section["items"] for section in jokic[0]["timeline"]}
        birthplace_labels = [item["label"] for item in timeline["birthplace"]]
        self.assertEqual(birthplace_labels, ["Sombor, Sombor City"])
        jokic_pro_labels = [item["label"] for item in timeline["pro"]]
        self.assertIn("Denver Nuggets / Ball Arena", jokic_pro_labels)
        self.assertNotIn("NBA team 7 / Ball Arena", jokic_pro_labels)

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_nba_venue_timeline_uses_team_names_not_numeric_ids(self) -> None:
        query = viz.normalize_query(
            {
                "place": "Sombor, Serbia",
                "lat": 45.78,
                "lon": 19.12,
                "radius_mi": 75,
                "pro_start_year": 1970,
                "pro_end_year": 2026,
                "birth_start_year": 1800,
                "birth_end_year": 2026,
                "sports": ["NBA"],
                "groups": [{"clauses": [{"kind": "birthplace"}]}],
            }
        )
        response = viz.build_response(query)
        topic = [row for row in response["players"] if row["sport"] == "NBA" and row["display_name"] == "Nikola Topic"]
        self.assertEqual(len(topic), 1)
        timeline = {section["key"]: section["items"] for section in topic[0]["timeline"]}
        pro_labels = [item["label"] for item in timeline["pro"]]
        self.assertIn("Oklahoma City Thunder / Paycom Center", pro_labels)
        self.assertNotIn("NBA team 25 / Paycom Center", pro_labels)

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_mlb_timeline_uses_team_ids_for_pro_park_events(self) -> None:
        rows = [{"sport": "MLB", "player_id": "oelkebr01", "display_name": "Bryan Oelkers"}]
        viz.attach_player_timelines(rows)
        timeline = {section["key"]: section["items"] for section in rows[0]["timeline"]}
        pro_labels = [item["label"] for item in timeline["pro"]]
        self.assertIn("Minnesota Twins / Hubert H. Humphrey Metrodome", pro_labels)
        self.assertIn("Cleveland Indians / Cleveland Stadium", pro_labels)
        self.assertNotIn("Hubert H. Humphrey Metrodome (MIN03)", pro_labels)

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_nba_timeline_prefers_team_venue_rows_over_duplicate_team_only_rows(self) -> None:
        rows = [{"sport": "NBA", "player_id": "690", "display_name": "Zeljko Rebraca"}]
        viz.attach_player_timelines(rows)
        timeline = {section["key"]: section["items"] for section in rows[0]["timeline"]}
        pro_labels = [item["label"] for item in timeline["pro"]]
        self.assertIn("Detroit Pistons / The Palace of Auburn Hills", pro_labels)
        self.assertIn("LA Clippers / crypto.com Arena", pro_labels)
        self.assertIn("Atlanta Hawks", pro_labels)
        self.assertNotIn("Detroit Pistons", pro_labels)
        self.assertNotIn("Los Angeles Clippers", pro_labels)

    @unittest.skipUnless((ROOT / "scratch" / "pro_venue_stints.sqlite").exists(), "local pro venue cache is not present")
    def test_canonical_pro_venue_stints_cover_cached_source_ranges(self) -> None:
        con = sqlite3.connect(ROOT / "scratch" / "pro_venue_stints.sqlite")
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                select sport, team_name, venue_name, start_year, end_year
                from pro_venue_stints
                where (sport = 'MLB' and team_name = 'Minnesota Twins' and venue_name = 'Hubert H. Humphrey Metrodome')
                   or (sport = 'NBA' and team_name = 'Denver Nuggets' and venue_name = 'Ball Arena')
                   or (sport = 'NFL' and team_name = 'San Francisco 49ers' and venue_name = '3Com Park')
                   or (sport = 'NFL' and team_name = 'San Francisco 49ers' and venue_name = 'Candlestick Park')
                   or (sport = 'NFL' and team_name = 'Las Vegas Outlaws' and venue_name = 'Sam Boyd Stadium')
                """
            ).fetchall()
        finally:
            con.close()

        observed: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            observed.setdefault((row["sport"], row["team_name"], row["venue_name"]), []).append(row)
        twins = observed[("MLB", "Minnesota Twins", "Hubert H. Humphrey Metrodome")]
        nuggets = observed[("NBA", "Denver Nuggets", "Ball Arena")]
        niners = observed[("NFL", "San Francisco 49ers", "3Com Park")]
        candlestick = observed[("NFL", "San Francisco 49ers", "Candlestick Park")]
        outlaws = observed[("NFL", "Las Vegas Outlaws", "Sam Boyd Stadium")]
        self.assertTrue(any((row["start_year"], row["end_year"]) == (1982, 2009) for row in twins))
        self.assertTrue(any(row["start_year"] <= 2002 and row["end_year"] >= 2026 for row in nuggets))
        self.assertTrue(any((row["start_year"], row["end_year"]) == (1999, 2004) for row in niners))
        self.assertTrue(any((row["start_year"], row["end_year"]) == (1971, 1998) for row in candlestick))
        self.assertTrue(any((row["start_year"], row["end_year"]) == (2001, 2001) for row in outlaws))

    @unittest.skipUnless((ROOT / "scratch" / "pro_venue_stints.sqlite").exists(), "local pro venue cache is not present")
    def test_canonical_pro_venue_lookup_augments_team_only_items_when_unambiguous(self) -> None:
        self.assertEqual(
            viz.canonical_venue_for_team_years("NFL", "San Francisco 49ers", "1999"),
            "3Com Park",
        )
        self.assertEqual(
            viz.canonical_venue_for_team_years("NFL", "San Francisco 49ers", "1993"),
            "Candlestick Park",
        )
        self.assertEqual(
            viz.canonical_venue_for_team_years("NFL", "Las Vegas Outlaws", "2001"),
            "Sam Boyd Stadium",
        )
        self.assertEqual(
            viz.canonical_venue_for_team_years("NFL", "Houston Oilers", "1988"),
            "Astrodome",
        )
        self.assertEqual(
            viz.canonical_venue_for_team_years("NFL", "Seattle Seahawks", "1989"),
            "Kingdome",
        )
        self.assertEqual(
            viz.canonical_venue_for_team_years("NFL", "Kansas City Chiefs", "1994-1998"),
            "Arrowhead Stadium",
        )
        self.assertEqual(
            viz.canonical_venue_for_team_years("NFL", "Oakland Raiders", "2007"),
            "McAfee Coliseum",
        )
        self.assertEqual(viz.nfl_team_name_for_season("LV", 2007), "Oakland Raiders")
        self.assertEqual(viz.nfl_team_name_for_season("LV", 2020), "Las Vegas Raiders")
        self.assertEqual(viz.nfl_team_name_for_season("LAR", 2007), "St. Louis Rams")
        self.assertEqual(viz.nfl_team_name_for_season("LAC", 2007), "San Diego Chargers")
        self.assertEqual(viz.nfl_team_name_for_season("WAS", 2019), "Washington Redskins")
        self.assertEqual(viz.nfl_team_name_for_season("WAS", 2020), "Washington Football Team")
        self.assertEqual(
            viz.canonical_venue_for_team_years("NBA", "Denver Nuggets", "2016-2026"),
            "Ball Arena",
        )
        self.assertIsNone(viz.canonical_venue_for_team_years("NFL", "San Francisco 49ers", "Years NA"))

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_eric_frampton_timeline_uses_oakland_raiders_venue(self) -> None:
        rows = [{"sport": "NFL", "player_id": "00-0025552", "display_name": "Eric Frampton"}]
        viz.attach_player_timelines(rows)
        timeline = {section["key"]: section["items"] for section in rows[0]["timeline"]}
        pro_labels = [item["label"] for item in timeline["pro"]]
        self.assertIn("Oakland Raiders / McAfee Coliseum", pro_labels)
        self.assertNotIn("Oakland Raiders", pro_labels)

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_jethro_franklin_timeline_hides_school_and_adds_historical_venues(self) -> None:
        rows = [{"sport": "NFL", "player_id": "FRA474588", "display_name": "Jethro Franklin"}]
        viz.attach_player_timelines(rows)
        timeline = {section["key"]: section["items"] for section in rows[0]["timeline"]}
        self.assertNotIn("school", timeline)
        pro_labels = [item["label"] for item in timeline["pro"]]
        self.assertIn("Houston Oilers / Astrodome", pro_labels)
        self.assertIn("Seattle Seahawks / Kingdome", pro_labels)
        self.assertNotIn("Houston Oilers", pro_labels)
        self.assertNotIn("Seattle Seahawks", pro_labels)

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_struer_denmark_birthplace_query_finds_morten_andersen(self) -> None:
        con = viz.connect()
        try:
            morten_birthplace = con.execute(
                """
                select l.label, l.country, e.start_year
                from geocoded_player_location_events e
                join locations l using (location_id)
                where e.sport = 'NFL'
                  and e.player_id = '00-0000282'
                  and e.event_type = 'born'
                """
            ).fetchone()
        finally:
            con.close()

        self.assertIsNotNone(morten_birthplace)
        self.assertEqual(morten_birthplace["label"], "Struer Municipality, Central Denmark")
        self.assertEqual(morten_birthplace["country"], "Denmark")
        self.assertEqual(morten_birthplace["start_year"], 1960)

        query = viz.normalize_query(
            {
                "place": "Struer, Denmark",
                "lat": 56.504,
                "lon": 8.599,
                "radius_mi": 25,
                "pro_start_year": 1970,
                "pro_end_year": 2026,
                "birth_start_year": 1800,
                "birth_end_year": 2026,
                "sports": ["NFL"],
                "groups": [{"clauses": [{"kind": "birthplace"}]}],
            }
        )
        response = viz.build_response(query)
        morten = [row for row in response["players"] if row["sport"] == "NFL" and row["player_id"] == "00-0000282"]
        self.assertEqual(len(morten), 1)
        self.assertEqual(morten[0]["display_name"], "Morten Andersen")
        self.assertIn("Struer Municipality, Central Denmark", morten[0]["matched_locations"])

    @unittest.skipUnless(viz.DB_PATH.exists(), "local scratch database is not present")
    def test_mac_jones_birthplace_uses_curated_wikipedia_override(self) -> None:
        con = viz.connect()
        try:
            birthplace = con.execute(
                """
                select l.label, l.country, e.source
                from player_location_events e
                join locations l using (location_id)
                where e.sport = 'NFL'
                  and e.player_id = '00-0036972'
                  and e.event_type = 'born'
                """
            ).fetchone()
        finally:
            con.close()

        self.assertIsNotNone(birthplace)
        self.assertEqual(birthplace["label"], "Jacksonville, FL")
        self.assertEqual(birthplace["country"], "USA")
        self.assertIn("curated Wikidata conflict override", birthplace["source"])

        jacksonville_query = viz.normalize_query(
            {
                "place": "Jacksonville, FL",
                "lat": 30.336864,
                "lon": -81.661603,
                "radius_mi": 25,
                "pro_start_year": 1970,
                "pro_end_year": 2026,
                "birth_start_year": 1800,
                "birth_end_year": 2026,
                "sports": ["NFL"],
                "groups": [{"clauses": [{"kind": "birthplace"}]}],
            }
        )
        france_query = viz.normalize_query(
            {
                "place": "Les Angles, France",
                "lat": 43.082777777,
                "lon": 0.006944444,
                "radius_mi": 25,
                "pro_start_year": 1970,
                "pro_end_year": 2026,
                "birth_start_year": 1800,
                "birth_end_year": 2026,
                "sports": ["NFL"],
                "groups": [{"clauses": [{"kind": "birthplace"}]}],
            }
        )
        jacksonville_players = [
            row for row in viz.build_response(jacksonville_query)["players"] if row["player_id"] == "00-0036972"
        ]
        france_players = [row for row in viz.build_response(france_query)["players"] if row["player_id"] == "00-0036972"]
        self.assertEqual(len(jacksonville_players), 1)
        self.assertEqual(france_players, [])


if __name__ == "__main__":
    unittest.main()
