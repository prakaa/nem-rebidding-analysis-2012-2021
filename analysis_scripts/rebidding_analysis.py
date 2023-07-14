import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import matplotlib.pyplot as plt
import pandas as pd
import polars as pl
from tqdm import tqdm


def get_gen_tech_mapping(path_to_mappings: Path) -> pd.DataFrame:
    gen_loads = pd.read_csv(path_to_mappings / Path("cleaned_gen_loads.csv"))
    non_gen_loads = pd.read_csv(
        path_to_mappings / Path("non_genloads_duid_providers_with_techs.csv")
    )
    simple_techs = pd.read_json(
        path_to_mappings / Path("techtype_simple_mapping.json"), typ="series"
    )
    combined = pd.concat([gen_loads, non_gen_loads], axis=0)
    combined = combined.replace(simple_techs)
    combined.rename(
        columns={"Technology Type - Descriptor": "Tech"}, inplace=True
    )
    keep_cols = [
        "Participant",
        "Region",
        "DUID",
        "Station Name",
        "Tech",
        "Reg Cap (MW)",
        "Colour",
    ]
    combined = combined[keep_cols]
    return combined


def get_bid_data_for_periods(
    partitioned_data_path: Path,
    day_col: str,
    day: datetime,
    period_start: int,
    period_end: int,
) -> pd.DataFrame:
    """
    Day should be a datetime with day, year and month
    NEM day starts at 4AM, hence add 4 hours in addition to PERIODID
    """
    q = pl.scan_parquet(
        partitioned_data_path
        / Path(day_col)
        / Path(day.strftime("%Y%m%d") + "*.parquet")
    ).filter(
        (
            pl.col("PERIODID").is_between(
                period_start, period_end, closed="both"
            )
        )
    )
    df = q.collect()
    df = df.to_pandas()
    df[day_col + "TIME"] = (
        df[day_col]
        + pd.Timedelta(minutes=5) * df.PERIODID
        + pd.Timedelta(hours=4)
    )
    offer_col = [col for col in df.columns if "OFFERDATE" in col].pop()
    df["REBIDAHEADTIME"] = df[day_col + "TIME"] - df[offer_col]
    return df


def get_all_rebids_less_than_ahead_time(
    df: pd.DataFrame, ahead_time: timedelta
) -> pd.DataFrame:
    """
    There appears to be a large number of bids submitted after the dispatch interval
    of interest. Hence the lower bound of `> pd.Timedelta(minutes=0)`.

    This method will still capture bids that might have been submitted after
    (informal) gate closure.
    """
    filtered = df[df["REBIDAHEADTIME"] > pd.Timedelta(minutes=0)]
    filtered = filtered[filtered["REBIDAHEADTIME"] <= ahead_time]
    return filtered


def count_rebids_by_tech(
    df: pd.DataFrame, ahead_time: timedelta, path_to_mappings: Path
) -> pd.DataFrame:
    """
    For a set of bids at a particular offer time, we drop duplicates for a DUID
    to avoid treating energy and FCAS bids submitted at the same time as different bids
    """
    mapping = get_gen_tech_mapping(path_to_mappings)
    filtered = get_all_rebids_less_than_ahead_time(df, ahead_time)
    merged = pd.merge(
        filtered,
        mapping,
        on="DUID",
        how="left",
    )
    merged["Tech"].fillna("Unknown", inplace=True)
    rebid_cols = [col for col in merged.columns if "TIME" in col] + [
        "DUID",
        "Tech",
    ]
    rebid = merged[rebid_cols].drop_duplicates()
    rebid = rebid.groupby("Tech")["DUID"].count().rename("REBIDS")
    return rebid


def rebid_counts_across_day(
    partitioned_data_path: Path,
    path_to_mappings: Path,
    trading_year: int,
    trading_month: int,
    trading_day: int,
    ahead_time: timedelta,
) -> pd.DataFrame:
    trading_date = datetime(trading_year, trading_month, trading_day)
    if trading_date < datetime(2021, 3, 1):
        day_col = "SETTLEMENTDATE"
    else:
        day_col = "TRADINGDATE"
    df = get_bid_data_for_periods(
        partitioned_data_path,
        day_col,
        trading_date,
        1,
        288,
    )
    counts = {}
    for period_id in range(1, 289):
        trading_datetime = trading_date + pd.Timedelta(
            hours=4, minutes=(5 * period_id)
        )
        period_df = df[df.PERIODID == period_id]
        period_counts = count_rebids_by_tech(
            period_df, ahead_time, path_to_mappings
        )
        counts[trading_datetime] = period_counts
    counts = pd.DataFrame.from_dict(counts, orient="index")
    return counts


def rebid_counts_across_july(
    years: List[int],
    ahead_time: timedelta,
    partitioned_data_path: Path,
    mappings_path: Path,
    output_path: Path,
) -> None:
    month = 7
    ahead_seconds = int(ahead_time.total_seconds())
    for year in years:
        logging.info(f"Processing {year}")
        month_data: List[pd.DataFrame] = []
        for day in tqdm(range(1, 31), desc=f"Processing {year}"):
            try:
                day_count = rebid_counts_across_day(
                    partitioned_data_path,
                    mappings_path,
                    year,
                    month,
                    day,
                    ahead_time,
                )
            except FileNotFoundError:
                logging.warning(
                    f"No data for {day}/{month}/{year}. Continuing"
                )
                continue
            month_data.append(day_count)
        month_df = pd.concat(month_data, axis=0)
        month_df.to_parquet(
            output_path
            / Path(
                f"rebid_counts_{month}_{year}_{ahead_seconds}.parquet",
            )
        )


def plot_rebid_counts_across_july(output_path: Path, path_to_mappings: Path):
    (month, month_str) = (7, "July")
    data: List[pd.DataFrame] = []
    for file in output_path.glob(f"rebid_counts_{month}*.parquet"):
        df_month = pd.read_parquet(file)
        data.append(df_month)
    df = pd.concat(data, axis=0)
    df["Year"] = df.index.year
    by_year = df.groupby("Year").sum()
    percent_by_year = by_year.div(by_year.sum(axis=1), axis=0) * 100
    year_totals = by_year.sum(axis=1)
    tech_colors = pd.read_json(
        path_to_mappings / Path("color_techtype_mapping.json"), typ="series"
    )
    fig, ax = plt.subplots(1, 1, figsize=(8, 6))
    last_col = None
    offset = pd.Series()
    for col in percent_by_year:
        value = percent_by_year[col]
        if last_col is not None:
            ax.bar(
                percent_by_year.index,
                value,
                bottom=offset,
                label=col,
                color=tech_colors[col],
            )
            offset = offset + value
        else:
            ax.bar(
                percent_by_year.index, value, label=col, color=tech_colors[col]
            )
            offset = value
        for index, item in value.items():
            if item > 3:
                ax.text(
                    index,
                    (offset[index] - item / 2),
                    f"{int(item)}%",
                    c="white",
                    ha="center",
                    va="center",
                    fontsize=9,
                )
        last_col = col
    for index, item in year_totals.items():
        ax.text(
            index,
            107,
            f"{int(item)}",
            horizontalalignment="center",
            fontsize=12,
        )
    ax.legend(
        bbox_to_anchor=(0.5, -0.25),
        loc="lower center",
        borderaxespad=0,
        frameon=False,
        ncol=4,
    )
    ax.set_ylabel(
        f"Percentage of all rebids within 5 minutes in {month_str} (%)"
    )
    ax.set_title(
        f"Share of all rebids within 5 minutes of every DI in {month_str}, 2013-2021",
        pad=25,
    )
    fig.savefig(Path("plots", "rebids_july_share_by_tech_2013_2021.pdf"))


def main():
    logging.basicConfig(level=logging.INFO)
    plt.style.use(Path("plots", "matplotlibrc.mplstyle"))
    partitioned_path = Path("data", "partitioned")
    mappings_path = Path("data", "mappings")
    output_path = Path("data", "processed")
    if not output_path.exists():
        output_path.mkdir()
    rebid_counts_across_july(
        list(range(2013, 2023, 2)),
        timedelta(minutes=5),
        partitioned_path,
        mappings_path,
        output_path,
    )
    plot_rebid_counts_across_july(
        Path("data", "processed"), Path("data", "mappings")
    )


if __name__ == "__main__":
    main()
