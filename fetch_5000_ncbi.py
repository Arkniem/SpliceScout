"""
Stage 1 — FETCH: get all study IDs + sample data from NCBI GEO for a query.
Paginated esearch + batched esummary, rate-limited. Writes raw JSON + unique titles
into a run directory (paths from pipeline_paths.Paths).
"""
import urllib.request
import urllib.parse
import json
import time
import os

from progress import NULL


def _key_suffix(ncbi_key):
    return f"&api_key={ncbi_key}" if ncbi_key else ""


def _pace(ncbi_key):
    # NCBI: 10 req/s with key, 3 req/s without
    return 0.11 if ncbi_key else 0.34


def search_all_ids(query, db="gds", max_results=5000, ncbi_key=None):
    """Fetch study IDs, paginating up to max_results (or all if max_results huge)."""
    all_ids, retstart, batch = [], 0, 500
    total = max_results
    while retstart < max_results:
        url = (f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
               f"?db={db}&term={urllib.parse.quote(query)}&retmode=json"
               f"&retmax={min(batch, max_results - retstart)}&retstart={retstart}"
               f"{_key_suffix(ncbi_key)}")
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                ids = data['esearchresult']['idlist']
                total = int(data['esearchresult']['count'])
                all_ids.extend(ids)
                print(f"  Fetched IDs {retstart}-{retstart+len(ids)} of {total}")
                break
            except Exception as e:
                print(f"  esearch error at retstart={retstart}: {e}; retrying...")
                time.sleep(2 * (attempt + 1))
        retstart += batch
        if retstart >= total or not ids:
            break
        time.sleep(_pace(ncbi_key))
    return all_ids[:max_results]


def fetch_summaries_batch(id_list, db="gds", batch_size=50, ncbi_key=None, reporter=NULL):
    """Fetch esummary for all IDs in batches."""
    all_results = {}
    total_batches = (len(id_list) + batch_size - 1) // batch_size
    reporter.set_total(total_batches)
    for i in range(0, len(id_list), batch_size):
        batch_ids = id_list[i:i + batch_size]
        url = (f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
               f"?db={db}&id={','.join(batch_ids)}&retmode=json{_key_suffix(ncbi_key)}")
        batch_num = i // batch_size + 1
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                if 'result' in data:
                    all_results.update(data['result'])
                    if batch_num % 10 == 0 or batch_num == total_batches:
                        print(f"  Summary batch {batch_num}/{total_batches}")
                break
            except Exception as e:
                wait = 2 ** attempt
                print(f"  esummary error batch {batch_num}: {e}; waiting {wait}s...")
                time.sleep(wait)
        reporter.advance(1)
        reporter.set_detail(f"summaries {batch_num}/{total_batches}")
        time.sleep(_pace(ncbi_key))
    return all_results


def run(query, max_results, P, ncbi_key=None, reporter=NULL):
    """Stage 1 entry point. Writes P.raw_json and P.unique_titles."""
    print(f"=== FETCH: GEO studies for {query!r} (cap={max_results}) ===")
    reporter.set_detail("searching GEO study IDs…")
    ids = search_all_ids(query, max_results=max_results, ncbi_key=ncbi_key)
    print(f"  Total IDs: {len(ids)}")
    if not ids:
        raise RuntimeError("No results found for query")

    reporter.set_detail(f"{len(ids)} studies found; fetching summaries…")
    summaries = fetch_summaries_batch(ids, batch_size=50, ncbi_key=ncbi_key, reporter=reporter)

    unique_titles, total_samples, study_count = set(), 0, 0
    raw_data = {"uids": ids}
    for uid in ids:
        if uid in summaries:
            item = summaries[uid]
            raw_data[uid] = item
            for sample in item.get('samples', []):
                total_samples += 1
                t = sample.get('title', '')
                if t:
                    unique_titles.add(t)
            study_count += 1

    with open(P.raw_json, "w", encoding="utf-8") as f:
        json.dump({"result": raw_data}, f)
    with open(P.unique_titles, "w", encoding="utf-8") as f:
        json.dump(sorted(unique_titles), f)
    print(f"  Studies with data: {study_count} | samples: {total_samples} | "
          f"unique titles: {len(unique_titles)}")
    print(f"  Wrote {P.raw_json}")
    return {"studies": study_count, "samples": total_samples, "ids": len(ids)}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="rna-seq[Description] AND human[Organism] AND drug")
    ap.add_argument("--max", type=int, default=5000)
    ap.add_argument("--run-dir", default=os.path.join("runs", "_adhoc"))
    ap.add_argument("--ncbi-key", default=None)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    run(a.query, a.max, P, a.ncbi_key)


if __name__ == "__main__":
    main()
