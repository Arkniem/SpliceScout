"""Single source of truth for every file path in a pipeline run.

All pipeline scripts derive their paths from a `Paths(run_dir)` object instead of
module-level hardcoded constants, so the whole pipeline is parameterized by an output
directory (one folder per query run).
"""
import os


class Paths:
    def __init__(self, run_dir):
        self.run_dir = os.path.abspath(run_dir)

        # config / state / logs
        self.config = self._p("config.json")
        self.state = self._p("pipeline_state.json")
        self.cost_log = self._p("cost_log.jsonl")

        # stage 1: fetch
        self.raw_json = self._p("ncbi_raw.json")
        self.unique_titles = self._p("unique_titles.json")

        # stage 2: structured extract + protocol
        self.samples_jsonl = self._p("structured_samples.jsonl")
        self.done_set = self._p("structured_done.json")
        self.study_protocol = self._p("study_protocol.json")

        # stage 3: AI prep (batch inputs) + stage 4/5 results
        self.work_dir = self._p("ai_work")
        self.compound_batches = os.path.join(self.work_dir, "compound_batches")
        self.compound_results = os.path.join(self.work_dir, "compound_results")
        self.sample_batches = os.path.join(self.work_dir, "sample_batches")
        self.sample_results = os.path.join(self.work_dir, "sample_results")
        self.sample_index = os.path.join(self.work_dir, "sample_index.json")

        # stage 6: merged AI maps
        self.compound_map = self._p("compound_map.json")
        self.sample_map = self._p("sample_map.json")
        self.pending = self._p("pending.json")

        # stage 7: output tables
        self.tables_dir = self._p("tables")
        self.final_csv = os.path.join(self.tables_dir, "ncbi_final.csv")
        self.final_md = os.path.join(self.tables_dir, "ncbi_final.md")
        self.protocol_audit = os.path.join(self.tables_dir, "ncbi_protocol_audit.csv")

        # stage 7 also persists a per-cell-line index (full studies + GSMs, no truncation)
        self.cellline_index = self._p("cellline_index.json")

        # stages 8-12: deep dive (Run Selector metadata for the single best cell line)
        self.runtable_dir = self._p("runtable")
        self.xml_cache_dir = os.path.join(self.runtable_dir, "xml_cache")
        self.by_study_dir = os.path.join(self.runtable_dir, "by_study")
        self.cellline_selection = os.path.join(self.runtable_dir, "cellline_selection.json")
        self.match_candidates = os.path.join(self.runtable_dir, "match_candidates.json")
        self.cellline_match = os.path.join(self.runtable_dir, "cellline_match.json")
        self.runtable_all_csv = os.path.join(self.runtable_dir, "SraRunTable_all.csv")
        self.sra_acc_list = os.path.join(self.runtable_dir, "SraAccList.txt")  # MAIN deep-dive output
        self.runtable_drug_review = os.path.join(self.runtable_dir, "drug_annotation_review.csv")

        # stages 13-14: cluster download handoff
        self.cluster_dir = os.path.join(self.runtable_dir, "cluster")          # bundle root
        self.cluster_bundle_zip = os.path.join(self.runtable_dir, "cluster_bundle.zip")

    def runtable_filtered_csv(self, slug):
        return os.path.join(self.runtable_dir, f"SraRunTable_{slug}.csv")

    def runtable_workbook(self, slug):
        return os.path.join(self.runtable_dir, f"SraRunTable_{slug}.xlsx")

    def _p(self, name):
        return os.path.join(self.run_dir, name)

    def ensure_dirs(self):
        for d in (self.run_dir, self.work_dir, self.compound_batches,
                  self.compound_results, self.sample_batches, self.sample_results,
                  self.tables_dir, self.runtable_dir, self.xml_cache_dir, self.cluster_dir):
            os.makedirs(d, exist_ok=True)
        return self
