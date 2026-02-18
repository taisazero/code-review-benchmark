use std::collections::HashMap;

use crate::model::*;

/// F-beta from precision and recall. Returns None if denominator is zero.
pub fn f_beta(precision: f64, recall: f64, beta: f32) -> Option<f64> {
    let b2 = (beta as f64) * (beta as f64);
    let denom = b2 * precision + recall;
    if denom <= 0.0 {
        None
    } else {
        Some((1.0 + b2) * precision * recall / denom)
    }
}

/// Check if a record passes all filters (except date range, which is handled by BTreeMap range).
fn record_matches(record: &PrRecord, snapshot: &Snapshot, params: &FilterParams) -> bool {
    // Chatbot filter
    if let Some(ref names) = params.chatbots {
        let info = &snapshot.chatbots[record.chatbot_idx as usize];
        let matches = names.iter().any(|n| {
            let n_lower = n.to_lowercase();
            info.github_username.to_lowercase() == n_lower
                || info.display_name.to_lowercase() == n_lower
        });
        if !matches {
            return false;
        }
    }

    // Language filter
    if let Some(ref langs) = params.languages {
        match record.language {
            Some(idx) => {
                let rec_lang = snapshot.languages[idx as usize].to_lowercase();
                if !langs.iter().any(|l| l.to_lowercase() == rec_lang) {
                    return false;
                }
            }
            None => return false, // no label → exclude when filter active
        }
    }

    // Domain filter
    if let Some(ref domains) = params.domains {
        match record.domain {
            Some(d) => {
                if !domains.contains(&d) {
                    return false;
                }
            }
            None => return false,
        }
    }

    // PR type filter
    if let Some(ref pr_types) = params.pr_types {
        match record.pr_type {
            Some(t) => {
                if !pr_types.contains(&t) {
                    return false;
                }
            }
            None => return false,
        }
    }

    // Severity filter
    if let Some(ref severities) = params.severities {
        match record.severity {
            Some(s) => {
                if !severities.contains(&s) {
                    return false;
                }
            }
            None => return false,
        }
    }

    // Diff lines range — records with diff_lines=None pass (matches dashboard default behavior)
    if let Some(dl) = record.diff_lines {
        if let Some(min) = params.diff_lines_min {
            if dl < min {
                return false;
            }
        }
        if let Some(max) = params.diff_lines_max {
            if dl > max {
                return false;
            }
        }
    }

    true
}

/// Collect all records in the date range that pass filters.
pub fn filter_records<'a>(snapshot: &'a Snapshot, params: &FilterParams) -> Vec<&'a PrRecord> {
    let mut results = Vec::new();

    // Determine which date buckets to scan
    match (params.start_date, params.end_date) {
        (Some(start), Some(end)) => {
            for (_date, records) in snapshot.by_date.range(start..=end) {
                for r in records {
                    if record_matches(r, snapshot, params) {
                        results.push(r);
                    }
                }
            }
        }
        (Some(start), None) => {
            for (_date, records) in snapshot.by_date.range(start..) {
                for r in records {
                    if record_matches(r, snapshot, params) {
                        results.push(r);
                    }
                }
            }
        }
        (None, Some(end)) => {
            for (_date, records) in snapshot.by_date.range(..=end) {
                for r in records {
                    if record_matches(r, snapshot, params) {
                        results.push(r);
                    }
                }
            }
        }
        (None, None) => {
            for records in snapshot.by_date.values() {
                for r in records {
                    if record_matches(r, snapshot, params) {
                        results.push(r);
                    }
                }
            }
            // Include no_date records when no date filter
            for r in &snapshot.no_date {
                if record_matches(r, snapshot, params) {
                    results.push(r);
                }
            }
        }
    }

    results
}

/// Accumulator for computing averages.
/// Tracks precision and recall counts separately — matches pandas mean() which skips NaN per column.
#[derive(Default)]
struct Accum {
    sum_precision: f64,
    precision_count: usize,
    sum_recall: f64,
    recall_count: usize,
}

/// Aggregate filtered records into daily metrics per chatbot.
/// Includes records where precision is non-None (matches dashboard behavior).
/// Recall is averaged separately over only the records that have it.
pub fn daily_metrics(snapshot: &Snapshot, params: &FilterParams) -> DailyMetricsResponse {
    let mut buckets: HashMap<(chrono::NaiveDate, u8), Accum> = HashMap::new();
    let mut seen_chatbots: std::collections::HashSet<u8> = std::collections::HashSet::new();

    // Iterate date range
    let iter: Box<dyn Iterator<Item = (&chrono::NaiveDate, &Vec<PrRecord>)>> =
        match (params.start_date, params.end_date) {
            (Some(start), Some(end)) => Box::new(snapshot.by_date.range(start..=end)),
            (Some(start), None) => Box::new(snapshot.by_date.range(start..)),
            (None, Some(end)) => Box::new(snapshot.by_date.range(..=end)),
            (None, None) => Box::new(snapshot.by_date.iter()),
        };

    for (date, records) in iter {
        for r in records {
            // Only require precision to be non-None (matches dashboard)
            let p = match r.precision {
                Some(p) => p,
                None => continue,
            };
            if !record_matches(r, snapshot, params) {
                continue;
            }
            let acc = buckets.entry((*date, r.chatbot_idx)).or_default();
            acc.sum_precision += p as f64;
            acc.precision_count += 1;
            if let Some(rc) = r.recall {
                acc.sum_recall += rc as f64;
                acc.recall_count += 1;
            }
            seen_chatbots.insert(r.chatbot_idx);
        }
    }

    let mut series: Vec<DailyMetricRow> = Vec::new();
    for ((date, chatbot_idx), acc) in &buckets {
        if acc.precision_count < params.min_prs_per_day.max(1) && params.min_prs_per_day > 0 {
            continue;
        }
        let avg_p = acc.sum_precision / acc.precision_count as f64;
        let (avg_r, avg_fb) = if acc.recall_count > 0 {
            let r = acc.sum_recall / acc.recall_count as f64;
            (r, f_beta(avg_p, r, params.beta))
        } else {
            // Match dashboard: pandas mean() of all-NaN recall → NaN → f_beta = None
            (0.0, None)
        };
        let info = &snapshot.chatbots[*chatbot_idx as usize];
        series.push(DailyMetricRow {
            date: *date,
            chatbot: info.github_username.clone(),
            avg_precision: avg_p,
            avg_recall: avg_r,
            avg_f_beta: avg_fb,
            pr_count: acc.precision_count,
        });
    }

    series.sort_by(|a, b| a.date.cmp(&b.date).then_with(|| a.chatbot.cmp(&b.chatbot)));

    let chatbots: Vec<String> = {
        let mut v: Vec<_> = seen_chatbots
            .iter()
            .map(|&idx| snapshot.chatbots[idx as usize].github_username.clone())
            .collect();
        v.sort();
        v
    };

    DailyMetricsResponse { chatbots, series }
}

/// Aggregate filtered records into one row per chatbot (leaderboard).
pub fn leaderboard(snapshot: &Snapshot, params: &FilterParams) -> LeaderboardResponse {
    let filtered = filter_records(snapshot, params);

    let mut accums: HashMap<u8, Accum> = HashMap::new();
    for r in &filtered {
        // Match dashboard: dropna(subset=["precision", "recall"]) — require both
        let p = match r.precision {
            Some(p) => p,
            None => continue,
        };
        let rc = match r.recall {
            Some(rc) => rc,
            None => continue,
        };
        let acc = accums.entry(r.chatbot_idx).or_default();
        acc.sum_precision += p as f64;
        acc.precision_count += 1;
        acc.sum_recall += rc as f64;
        acc.recall_count += 1;
    }

    let mut rows: Vec<LeaderboardRow> = accums
        .iter()
        .map(|(&idx, acc)| {
            let avg_p = acc.sum_precision / acc.precision_count as f64;
            let avg_r = if acc.recall_count > 0 {
                acc.sum_recall / acc.recall_count as f64
            } else {
                0.0
            };
            let info = &snapshot.chatbots[idx as usize];
            LeaderboardRow {
                chatbot: info.github_username.clone(),
                precision: avg_p,
                recall: avg_r,
                f_score: f_beta(avg_p, avg_r, params.beta),
                total_prs: acc.precision_count,
            }
        })
        .collect();

    rows.sort_by(|a, b| {
        b.f_score
            .partial_cmp(&a.f_score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    LeaderboardResponse { rows }
}

/// Extract available filter options from the snapshot.
pub fn filter_options(snapshot: &Snapshot) -> FilterOptionsResponse {
    let chatbots: Vec<String> = snapshot
        .chatbots
        .iter()
        .map(|c| c.github_username.clone())
        .collect();
    let mut languages = snapshot.languages.clone();
    languages.sort();

    let mut domains = std::collections::HashSet::new();
    let mut pr_types = std::collections::HashSet::new();
    let mut severities = std::collections::HashSet::new();

    let all = snapshot
        .by_date
        .values()
        .flat_map(|v| v.iter())
        .chain(snapshot.no_date.iter());
    for r in all {
        if let Some(d) = r.domain {
            domains.insert(format!("{:?}", d).to_lowercase());
        }
        if let Some(t) = r.pr_type {
            pr_types.insert(format!("{:?}", t).to_lowercase());
        }
        if let Some(s) = r.severity {
            severities.insert(format!("{:?}", s).to_lowercase());
        }
    }

    let sorted = |s: std::collections::HashSet<String>| -> Vec<String> {
        let mut v: Vec<_> = s.into_iter().collect();
        v.sort();
        v
    };

    FilterOptionsResponse {
        chatbots,
        languages,
        domains: sorted(domains),
        pr_types: sorted(pr_types),
        severities: sorted(severities),
        first_date: snapshot.by_date.keys().next().map(|d| d.to_string()),
        last_date: snapshot.by_date.keys().last().map(|d| d.to_string()),
    }
}
