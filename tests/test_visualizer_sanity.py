from __future__ import annotations

import gzip
import importlib.util
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VISUALIZER_PATH = ROOT / "scratch" / "pilot_visualizer_experiment.py"


def load_visualizer():
    spec = importlib.util.spec_from_file_location("pilot_visualizer_experiment", VISUALIZER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


viz = load_visualizer()


class VisualizerTaxonomyTests(unittest.TestCase):
    def test_college_layers_are_merged_and_school_is_hidden(self) -> None:
        self.assertNotIn("attended_school", viz.EVENT_TYPE_GROUPS.get("college", []))
        self.assertEqual(viz.EVENT_LAYER_BY_TYPE["attended_college"], "college")
        self.assertEqual(viz.EVENT_LAYER_BY_TYPE["played_college"], "college")
        self.assertEqual(viz.EVENT_LABELS["college"], "College")
        self.assertNotIn("School", set(viz.EVENT_LABELS.values()))
        self.assertNotIn("College Sports", set(viz.EVENT_LABELS.values()))

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
        ):
            self.assertIn(name, text)

    def test_text_page_escapes_content(self) -> None:
        page = viz.render_text_page("A < B", "credit: A & B < C").decode("utf-8")
        self.assertIn("A &lt; B", page)
        self.assertIn("A &amp; B &lt; C", page)


class RenderDeployTests(unittest.TestCase):
    def test_render_blueprint_declares_python_web_service(self) -> None:
        text = (ROOT / "render.yaml").read_text(encoding="utf-8")
        for snippet in (
            "type: web",
            "runtime: python",
            "python pipelines/render_build.py",
            "--host 0.0.0.0 --port $PORT",
            "healthCheckPath: /api/status",
        ):
            self.assertIn(snippet, text)
        build_script = (ROOT / "pipelines" / "render_build.py").read_text(encoding="utf-8")
        self.assertIn("pipelines/enrich_nba_alltime_wikidata.py", build_script)
        self.assertIn("HH_RENDER_INCLUDE_MEDIA", build_script)
        self.assertIn("data\" / \"derived", build_script)
        self.assertIn("player_media.sqlite.gz", build_script)

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


if __name__ == "__main__":
    unittest.main()
