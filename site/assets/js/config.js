/**
 * Build-time constants and frozen vocabularies.
 *
 * `BUILD`, `BASE_URL` and `MAINTAINER` are placeholders substituted by
 * `atlas/sitebuild.py`; the build fails if any of them survives into
 * `_site/`, so they are never seen by a browser.
 */

/** Short commit hash of the build, used as the `?v=` cache buster. */
export const BUILD = "__BUILD__";

/** Path prefix the site is served under, e.g. `/clinical-data-atlas/`. */
export const BASE_URL = "__BASE_URL__";

export const REPO_URL = "__REPO_URL__";

export const SITE_NAME = "Clinical Data Atlas";

export const MAINTAINER = "__MAINTAINER__";

/** The 17 clinical domains, in `atlas.vocab.DOMAINS` order. */
export const DOMAINS = [
  "neurology",
  "psychiatry",
  "neuroscience",
  "cardiology",
  "oncology",
  "pulmonology",
  "critical_care",
  "surgery",
  "pediatrics",
  "obstetrics_gynecology",
  "infectious_disease",
  "endocrinology_metabolism",
  "gastroenterology_hepatology",
  "nephrology_urology",
  "musculoskeletal",
  "public_health",
  "other",
];

/** Access tiers, least- to most-restrictive (`atlas.vocab.ACCESS_ORDER`). */
export const ACCESS = [
  "open",
  "registration",
  "credentialed",
  "application",
  "purchase",
];

/** Graph node types, in legend order. */
export const NODE_TYPES = [
  "source",
  "modality",
  "condition",
  "institution",
  "dataset",
];

/** How many neighbours a single node expansion may add to the scene. */
export const CAPS = { expand: 150, mobileExpand: 60 };
