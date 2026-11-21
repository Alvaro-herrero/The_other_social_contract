"""Compare five-year changes in satisfaction, electoral democracy, and PPP consumption.

Inputs are read only. Missing observations are estimated in memory within each
country; entirely missing series are excluded from the relevant comparison.
"""

import argparse
from pathlib import Path
from zipfile import BadZipFile

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
YEARS = (2005, 2010, 2015, 2020)
KEYS = ["country", "year"]
VALUES = ("satisfaction", "electoral_democracy", "ppp_consumption")
BOUNDS = {"satisfaction": (0, 100), "electoral_democracy": (0, 1),
          "ppp_consumption": (0, None)}
COUNTRY_ALIASES = {"czech republic": "CZE", "great britain": "GBR",
                   "macedonia (fyrom)": "MKD", "slovakia": "SVK", "turkey": "TUR"}
INPUT_FILES = {
    "eurobarometer": "eurobarometer_country_year_satisdms.csv",
    "latin_america": "Jamovi_Models1_3_Data.xlsx",
    "vdem": "V-Dem-CY-Core-v16.csv",
    "ppp": "API_NE.CON.PCAP.PP.KD_DS2_en_csv_v2_84798.csv",
}


def require_columns(frame, columns, path):
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")


def numeric(series):
    """Malformed numbers are errors; nonfinite measurements count as missing."""
    return pd.to_numeric(series, errors="raise").replace([np.inf, -np.inf], np.nan)


def validate_frame(frame, path, values=()):
    """Check country/year keys and convert measurements to their valid scales."""
    require_columns(frame, [*KEYS, *values], path)
    frame = frame.copy()
    frame["country"] = frame.country.astype("string").str.strip().str.upper()
    frame["year"] = numeric(frame.year)
    if (frame.country.isna().any() or frame.country.eq("").any()
            or frame.year.isna().any() or frame.year.mod(1).ne(0).any()):
        raise ValueError(f"{path}: country/year keys must be present, with integer years")
    if not frame.country.str.fullmatch(r"[A-Z]{3}").all():
        raise ValueError(f"{path}: country codes must have three letters")
    frame["year"] = frame.year.astype(int)
    if frame.duplicated(KEYS).any():
        raise ValueError(f"{path}: duplicate country/year keys")
    for value in values:
        frame[value] = numeric(frame[value])
        lower, upper = BOUNDS[value]
        outside = frame[value].dropna().lt(lower)
        if upper is not None:
            outside |= frame[value].dropna().gt(upper)
        if outside.any():
            raise ValueError(f"{path}: {value} outside its valid range {BOUNDS[value]}")
    return frame


def load_ppp(path):
    """Read the World Bank's metadata-prefixed, wide annual CSV."""
    wide = pd.read_csv(path, skiprows=4)
    year_columns = [str(year) for year in YEARS]
    require_columns(wide, ["Country Name", "Country Code", "Indicator Code", *year_columns], path)
    if not wide["Indicator Code"].eq("NE.CON.PCAP.PP.KD").all():
        raise ValueError(f"{path}: expected PPP consumption indicator NE.CON.PCAP.PP.KD")
    names = dict(zip(wide["Country Name"].str.strip().str.casefold(), wide["Country Code"]))
    names.update(COUNTRY_ALIASES)
    frame = wide.melt(id_vars="Country Code", value_vars=year_columns,
                      var_name="year", value_name="ppp_consumption")
    frame = frame.rename(columns={"Country Code": "country"})
    return validate_frame(frame, path, ["ppp_consumption"]), names


def load_eurobarometer(path, country_codes):
    """Unweighted percentage answering 1 or 2; zero rows are missing-year markers."""
    frame = pd.read_csv(path)
    require_columns(frame, ["country", "year", "satisdms"], path)
    names = frame.country.astype("string").str.strip()
    years = numeric(frame.year)
    if (names.isna().any() or names.eq("").any() or years.isna().any()
            or years.mod(1).ne(0).any()):
        raise ValueError(f"{path}: invalid survey country/year keys")
    responses = numeric(frame.satisdms)
    if not responses.dropna().isin([0, 1, 2, 3, 4]).all():
        raise ValueError(f"{path}: satisdms must be 1-4, missing, or a zero placeholder")
    frame = pd.DataFrame({"country_name": names, "year": years.astype(int),
                          "country": names.str.casefold().map(country_codes),
                          "response": responses.mask(responses.eq(0))})
    excluded = sorted(frame.loc[frame.country.isna(), "country_name"].unique().tolist())
    frame = frame.loc[frame.country.notna()].copy()
    frame["satisfied"] = frame.response.le(2).astype(float).where(frame.response.notna())
    frame = frame.groupby(KEYS, as_index=False).agg(
        satisfaction=("satisfied", "mean"), satisfaction_n=("response", "count"),
        country_name=("country_name", "first"))
    frame["satisfaction"] *= 100
    frame["region"], frame["source"] = "Europe", "Eurobarometer"
    return validate_frame(frame, path, ["satisfaction"]), excluded


def load_latin_america(path):
    with pd.ExcelFile(path) as workbook:
        sheet = "Jamovi_Data" if "Jamovi_Data" in workbook.sheet_names else 0
        frame = pd.read_excel(workbook, sheet_name=sheet)
    require_columns(frame, ["Country", "ISO3", "Year", "Satisfaction", "Satisfaction_N"], path)
    frame = frame.rename(columns={"Country": "country_name", "ISO3": "country",
                                  "Year": "year", "Satisfaction": "satisfaction",
                                  "Satisfaction_N": "satisfaction_n"})
    frame["satisfaction_n"] = numeric(frame.satisfaction_n)
    if frame.satisfaction_n.dropna().lt(0).any():
        raise ValueError(f"{path}: negative satisfaction sample count")
    frame["satisfaction"] = numeric(frame.satisfaction).where(frame.satisfaction_n.gt(0))
    frame["region"], frame["source"] = "Latin America", "Latinobarometro"
    columns = ["country", "year", "country_name", "region", "source", "satisfaction", "satisfaction_n"]
    return validate_frame(frame[columns], path, ["satisfaction"])


def load_vdem(path):
    frame = pd.read_csv(path, usecols=["country_text_id", "year", "v2x_polyarchy"])
    frame = frame.rename(columns={"country_text_id": "country", "v2x_polyarchy": "electoral_democracy"})
    return validate_frame(frame, path, ["electoral_democracy"])


def estimate_series(years, values, bounds):
    """Interpolate gaps and extrapolate ends from the nearest two observed years.

    One observed point permits only a constant estimate. With no observations,
    the series stays missing. Only estimated values are clipped to valid bounds.
    """
    years, values = np.asarray(years, dtype=float), np.asarray(values, dtype=float)
    if np.any(np.diff(years) <= 0):
        raise ValueError("Estimation requires distinct, increasing years")
    filled = values.copy()
    known = np.isfinite(values)
    methods = np.where(known, "observed", "unavailable").astype(object)
    clipped = np.zeros(len(values), dtype=bool)
    if not known.any() or known.all():
        return filled, methods, clipped
    x, y = years[known], values[known]
    missing = ~known
    if len(x) == 1:
        filled[missing], methods[missing] = y[0], "constant"
    else:
        filled[missing] = np.interp(years[missing], x, y)
        methods[missing] = "interpolated"
        left, right = missing & (years < x[0]), missing & (years > x[-1])
        filled[left] = y[0] + (years[left] - x[0]) * (y[1] - y[0]) / (x[1] - x[0])
        filled[right] = y[-1] + (years[right] - x[-1]) * (y[-1] - y[-2]) / (x[-1] - x[-2])
        methods[left | right] = "extrapolated"
    bounded = np.clip(filled[missing], *bounds)
    clipped[missing] = bounded != filled[missing]
    filled[missing] = bounded
    return filled, methods, clipped


def impute_panel(panel):
    panel = panel.sort_values(KEYS).reset_index(drop=True).copy()
    for value in VALUES:
        panel[f"{value}_method"] = "unavailable"
        panel[f"{value}_clipped"] = False
        for indices in panel.groupby("country").groups.values():
            filled, methods, clipped = estimate_series(
                panel.loc[indices, "year"], panel.loc[indices, value], BOUNDS[value])
            panel.loc[indices, value] = filled
            panel.loc[indices, f"{value}_method"] = methods
            panel.loc[indices, f"{value}_clipped"] = clipped
        panel[f"{value}_estimated"] = ~panel[f"{value}_method"].isin(["observed", "unavailable"])
    return panel


def calculate_changes(panel):
    """Later minus earlier, always using the same five-year endpoints."""
    panel = panel.sort_values(KEYS)
    grouped = panel.groupby("country")
    changes = panel[["country", "country_name", "region", "source", "year"]].rename(
        columns={"year": "end_year"}).copy()
    changes["start_year"] = grouped.year.shift()
    for value in VALUES:
        changes[f"{value}_change"] = grouped[value].diff()
        flag = f"{value}_estimated"
        changes[f"{value}_change_estimated"] = panel[flag] | grouped[flag].shift(fill_value=False)
    changes = changes.loc[changes.start_year.notna()].copy()
    changes["start_year"] = changes.start_year.astype(int)
    changes["years_elapsed"] = changes.end_year - changes.start_year
    if not changes.years_elapsed.eq(5).all():
        raise ValueError("Expected consecutive five-year intervals")
    return changes.reset_index(drop=True)


def correlate(x, y):
    """Pearson correlation, undefined for small or constant samples."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if len(x) < 3 or np.unique(x).size < 2 or np.unique(y).size < 2:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def comparison_data(changes, value):
    complete = changes.dropna(subset=["satisfaction_change", f"{value}_change"]).copy()
    complete["estimated"] = (complete.satisfaction_change_estimated
                              | complete[f"{value}_change_estimated"])
    return complete


def analyze(changes):
    rows = []
    for value in VALUES[1:]:
        complete = comparison_data(changes, value)
        x, y = complete.satisfaction_change, complete[f"{value}_change"]
        rows.append({"comparison": value, "intervals": len(complete),
                     "countries": complete.country.nunique(),
                     "estimated_intervals": int(complete.estimated.sum()),
                     "pearson": correlate(x, y)})
    return pd.DataFrame(rows)


def prepare_analysis(eurobarometer, latin_america, vdem, ppp):
    consumption, country_codes = load_ppp(ppp)
    europe, excluded = load_eurobarometer(eurobarometer, country_codes)
    latin = load_latin_america(latin_america)
    surveys = pd.concat([europe, latin], ignore_index=True)
    surveys = validate_frame(surveys, "Combined survey sources")
    surveys = surveys.loc[surveys.year.isin(YEARS)]
    metadata = surveys[["country", "country_name", "region", "source"]].drop_duplicates()
    if metadata.country.duplicated().any():
        raise ValueError("Ambiguous country names or overlapping survey sources")
    if metadata.empty:
        raise ValueError("No survey countries in the selected years")
    grid = pd.MultiIndex.from_product([sorted(metadata.country), YEARS], names=KEYS).to_frame(index=False)
    panel = grid.merge(metadata, on="country", validate="many_to_one")
    tables = [surveys[[*KEYS, "satisfaction", "satisfaction_n"]],
              load_vdem(vdem), consumption[[*KEYS, "ppp_consumption"]]]
    for table in tables:
        panel = panel.merge(table, on=KEYS, how="left", validate="one_to_one")
    panel = impute_panel(panel)
    changes = calculate_changes(panel)
    if len(changes) < 3:
        raise ValueError("Need at least three within-country change intervals")
    audit = {"excluded_survey_units": excluded,
             "ppp_unavailable": sorted(panel.loc[panel.ppp_consumption.isna(), "country_name"].unique().tolist()),
             "uk_proxy": "GBR" in set(panel.country)}
    return panel, changes, analyze(changes), audit


def make_graph(changes, results, output_path, audit):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.ticker import StrMethodFormatter

    labels = {"electoral_democracy": ("Electoral democracy vs satisfaction",
                                      "Change in V-Dem electoral democracy index (0-1)"),
              "ppp_consumption": ("PPP consumption vs satisfaction",
                                  "Change in consumption per capita\n(PPP, constant 2021 international $)")}
    number = lambda value: f"{value:.3f}" if np.isfinite(value) else "n/a"
    with plt.rc_context({"font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "savefig.facecolor": "white"}):
        fig, axes = plt.subplots(1, 2, figsize=(14, 6.5), sharex=True)
        try:
            fig.subplots_adjust(left=.075, right=.98, bottom=.27, top=.77, wspace=.30)
            for ax, value in zip(axes, VALUES[1:]):
                complete = comparison_data(changes, value)
                stats = results.loc[results.comparison.eq(value)].iloc[0]
                for estimated, marker, color in [(False, "o", "#356d9a"), (True, "D", "#c77725")]:
                    points = complete.loc[complete.estimated.eq(estimated)]
                    ax.scatter(points.satisfaction_change, points[f"{value}_change"],
                               marker=marker, color=color, alpha=.75, s=38,
                               edgecolor="white", linewidth=.5, zorder=3)
                ax.axhline(0, color="#888888", linewidth=.8)
                ax.axvline(0, color="#888888", linewidth=.8)
                ax.grid(alpha=.18)
                ax.set_axisbelow(True)
                ax.set_xlabel("Change in satisfaction (percentage points)", labelpad=9)
                ax.set_ylabel(labels[value][1], labelpad=9)
                ax.set_title(f"{labels[value][0]}\n"
                             f"{int(stats.intervals)} intervals / {int(stats.countries)} countries\n"
                             f"Correlation = {number(stats.pearson)}",
                             fontsize=11, pad=13)
                if complete.empty:
                    ax.text(.5, .5, "No complete change intervals", ha="center", transform=ax.transAxes)
                if value == "ppp_consumption":
                    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
            fig.suptitle("Correlation", fontsize=18, y=.97)
            fig.text(.5, .90, "2005-2010  |  2010-2015  |  2015-2020", ha="center", fontsize=11)
            handles = [Line2D([], [], marker="o", color="#356d9a", linestyle="none",
                              label="Observed endpoints"),
                       Line2D([], [], marker="D", color="#c77725", linestyle="none",
                              label="At least one estimated endpoint")]
            fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(.5, .12),
                       ncol=2, frameon=False)
            fig.text(.5, .095, "Each point is later minus earlier in the same country; changes are not annualized.",
                     ha="center", fontsize=9)
            fig.text(.5, .065, "Missing points: interpolation between observations; linear extrapolation at the ends, within valid bounds.",
                     ha="center", fontsize=9)
            if audit["ppp_unavailable"]:
                fig.text(.5, .032, "PPP comparison excludes series without observations: "
                         + ", ".join(audit["ppp_unavailable"]) + ".", ha="center", fontsize=9)
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output_path, dpi=200, format="jpeg", pil_kwargs={"quality": 95})
        finally:
            plt.close(fig)


def print_report(panel, changes, results, audit):
    print("Five-year changes: later minus earlier, within the same country.")
    print("Democracy: V-Dem v2x_polyarchy (expert-based electoral democracy, 0-1).")
    print("PPP: consumption per capita in constant 2021 international dollars.")
    print(f"Countries: {panel.country.nunique()}; country-years: {len(panel)}; intervals: {len(changes)}")
    for value in VALUES:
        counts = panel[f"{value}_method"].value_counts().to_dict()
        print(f"{value}: {counts}; estimates clipped to bounds: {int(panel[f'{value}_clipped'].sum())}")
    if audit["excluded_survey_units"]:
        print("Excluded survey units without corresponding national indicators: "
              + ", ".join(audit["excluded_survey_units"]))
    print(results.rename(columns={"pearson": "Correlation"}).to_string(
        index=False, float_format=lambda value: f"{value:.4f}"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name, filename in INPUT_FILES.items():
        parser.add_argument("--" + name.replace("_", "-"), type=Path, default=ROOT / "data" / filename)
    parser.add_argument("--output", type=Path, default=ROOT / "output/correlation.jpg")
    parser.add_argument("--dry-run", action="store_true", help="Calculate and report results without writing any output")
    args = parser.parse_args(argv)
    if args.output.suffix.lower() not in (".jpg", ".jpeg"):
        parser.error("--output must be a .jpg or .jpeg file")
    try:
        panel, changes, results, audit = prepare_analysis(
            args.eurobarometer, args.latin_america, args.vdem, args.ppp)
        print_report(panel, changes, results, audit)
        if not args.dry_run:
            make_graph(changes, results, args.output, audit)
            print(f"Saved {args.output.resolve()}")
    except (OSError, ValueError, ImportError, BadZipFile) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
