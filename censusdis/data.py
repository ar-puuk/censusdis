# Copyright (c) 2022 Darren Erik Vengroff
"""
Utilities for loading census data.

This module relies on the US Census API, which
it wraps in a pythonic manner.
"""

import warnings
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Generator,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)

import geopandas as gpd
import numpy as np
import pandas as pd

import censusdis.geography as cgeo
import censusdis.maps as cmap
from censusdis.impl.exceptions import CensusApiException
from censusdis.impl.fetch import data_from_url
from censusdis.impl.geometry import drop_slivers_from_gdf
from censusdis.impl.varcache import VariableCache
from censusdis.impl.varsource.base import VintageType
from censusdis.impl.varsource.censusapi import CensusApiVariableSource
from censusdis.values import ALL_SPECIAL_VALUES

logger = getLogger(__name__)


GeoFilterType = Optional[Union[str, Iterable[str]]]
"""
The type we accept for geographic filters.

They are used for the values of `kwargs` to
:py:func:`download`.

These filters are either single values as a string,
or, if multivalued, then an iterable containing all
the values allowed by the filter. For example::

    import censusdis.data as ced

    from censusdis.states import NJ, NY, CT

    # Two different kinds of kwarg for `state=`, both of
    # which are of `GeoFilterType`:
    df_one_state = ced.download("aca/acs5", 2020, ["NAME"], state=NJ)
    df_tri_state = ced.download("aca/acs5", 2020, ["NAME"], state=[NJ, NY, CT])
"""


def _gf2s(geo_filter: GeoFilterType) -> Optional[str]:
    """
    Convert a filter to a string.

    For the Census API, multiple values are encoded
    in a single comma separated string.
    """
    if geo_filter is None or isinstance(geo_filter, str):
        return geo_filter
    return ",".join(geo_filter)


_MAX_VARIABLES_PER_DOWNLOAD = 50
"""
The maximum number of variables we can ask for in one census API query.

The U.S. Census sets this limit, not us. In order to not expose our
users to the limit, :py:func:`~download` mostly obscures the fact that
requests to download more than this many variables are broken into
multiple calls to the census API and then the results are stitched back
together be either merging or concatenation. This is all handled in
:py:func:`~_download_multiple`.
"""


__dw_strategy_metrics = {"merge": 0, "concat": 0}
"""
Counters for how often we use each strategy for wide tables.
"""


def _download_wide_strategy_metrics() -> Dict[str, int]:
    """
    Metrics on which strategies have been used for wide tables.

    Returns
    -------
        A dictionary of metrics on how often each strategy has
        been used.
    """
    return dict(**__dw_strategy_metrics)


def _download_multiple(
    dataset: str,
    vintage: VintageType,
    download_variables: List[str],
    *,
    key: Optional[str],
    census_variables: "VariableCache",
    with_geometry: bool = False,
    row_keys: Optional[Union[str, Iterable[str]]] = None,
    **kwargs: cgeo.InSpecType,
) -> pd.DataFrame:
    """
    Download data in groups of columns and concatenate the results together.

    The reason for this function is that the API will only return a maximum
    of 50 columns per query. This function downloads wider data 50 columns
    at a time and concatenates them.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.
    download_variables
        The census variables to download, for example `["NAME", "B01001_001E"]`.
    with_geometry
        If `True` a :py:class:`gpd.GeoDataFrame` will be returned and each row
        will have a geometry that is a cartographic boundary suitable for platting
        a map. See https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.2020.html
        for details of the shapefiles that will be downloaded on your behalf to
        generate these boundaries.
    api_key
        An optional API key. If you don't have or don't use a key, the number
        of calls you can make will be limited.
    variable_cache
        A cache of metadata about variables.
    row_keys
        An optional set of identifier keys to help merge together requests for more than the census API limit of
        50 variables per query. These keys are useful for census datasets such as the Current Population Survey
        where the geographic identifiers do not uniquely identify each row.
    kwargs
        A specification of the geometry that we want data for.

    Returns
    -------
        The full results of the query with all columns.

    """
    # Divide the variables into groups. If row keys are provided, include them in each chunk of variables,
    # while respecting the variable max
    if row_keys:
        chunk_size = _MAX_VARIABLES_PER_DOWNLOAD - len(row_keys)
        variable_groups = [
            # black and flake8 disagree about the whitespace before ':' here...
            # We need to drop duplicates in each chunk of variables
            # since the row_key variables might already be present in one of the chunks
            [
                item
                for item in row_keys
                + download_variables[start : start + chunk_size]  # noqa: E203
                if item not in row_keys
                or row_keys.index(item)
                == (
                    row_keys
                    + download_variables[start : start + chunk_size]  # noqa: E203
                ).index(item)
            ]
            for start in range(0, len(download_variables), chunk_size)
        ]
    else:
        variable_groups = [
            # black and flake8 disagree about the whitespace before ':' here...
            download_variables[start : start + _MAX_VARIABLES_PER_DOWNLOAD]  # noqa: 203
            for start in range(0, len(download_variables), _MAX_VARIABLES_PER_DOWNLOAD)
        ]

    if len(variable_groups) < 2:
        raise ValueError(
            "_download_multiple expects to be called with at least "
            f"{_MAX_VARIABLES_PER_DOWNLOAD + 1} variables. With fewer,"
            "use download instead."
        )

    # Get the data for each chunk.
    dfs = [
        download(
            dataset,
            vintage,
            variable_group,
            api_key=key,
            variable_cache=census_variables,
            with_geometry=with_geometry and (ii == 0),
            **kwargs,
        )
        for ii, variable_group in enumerate(variable_groups)
    ]

    # What variables came back in the first df but were not
    # requested? These are a key to the geography the row
    # represents. For example, 'STATE' amd 'COUNTY' might
    # be these variables if we did a county-level query to
    # the census API.
    geo_key_variables = [f for f in dfs[0].columns if f not in set(variable_groups[0])]

    # If we put in the geometry column, it's not part of the
    # key.
    if with_geometry:
        geo_key_variables = [f for f in geo_key_variables if f != "geometry"]

    # Now we have to decide if we are going to use the merge
    # strategy or the concat strategy to combine the data frames
    # we downloaded. Why do we have two strategies? Because we are
    # dealing with two kinds of data. One kind, from data sets like
    # ACS (https://www.census.gov/programs-surveys/acs.html),
    # has a unique key of columns that specify geography. The other
    # kind, from data sets like CPS
    # (https://www.census.gov/programs-surveys/cps.html) doesn't.
    #
    # In the unique key case, we can join the data frames that come
    # back on those key columns and get the final wide data frame
    # we want for the user.
    #
    # In the non-unique key case, we can't do this. There data sets
    # may have multiple rows for a value of the key columns. We can't
    # join here. Instead, we can only concatenate the tables
    # horizontally and hope that the rows came back in the same order
    # for each of them.

    # We hope to be able to merge. It is safer. If row_keys is supplied, they are included in
    # merge keys
    merge_strategy = True
    if row_keys:
        merge_keys = geo_key_variables + row_keys
    else:
        merge_keys = geo_key_variables
    # But if there are any non-unique keys in any df, we can't
    # merge.
    for df_slice in dfs:
        if len(df_slice.value_counts(merge_keys, sort=False)) != len(df_slice.index):
            merge_strategy = False
            break

    if merge_strategy:
        # We can do the merge strategy.

        __dw_strategy_metrics["merge"] = __dw_strategy_metrics["merge"] + 1

        df_data = dfs[0]

        for df_right in dfs[1:]:
            df_data = df_data.merge(df_right, on=merge_keys)
    else:
        # We are going to have to fall back on the concat
        # strategy. Before we do the concat, however, let's
        # double-check that the key columns are the same in
        # at the corresponding row in every df. Otherwise, something
        # is fishy, and it is not safe to concat without mixing
        # data that should be in different rows.

        rows0 = len(dfs[0].index)

        for df_slice in dfs[1:]:
            if not (
                rows0 == len(df_slice.index)
                and dfs[0][geo_key_variables].equals(df_slice[geo_key_variables])
            ):
                # At least one difference. So we cannot use the
                # concat strategy either.
                if not row_keys:
                    raise CensusApiException(
                        "Neither the merge nor the concat strategy is viable. "
                        "We made multiple queries to the census API because more than "
                        f"{_MAX_VARIABLES_PER_DOWNLOAD} variables were requested. "
                        "If you don't need all the variables, it is always safer to "
                        f"download less than {_MAX_VARIABLES_PER_DOWNLOAD} variables. "
                        f"If you need more than {_MAX_VARIABLES_PER_DOWNLOAD}, you can supply the `row_keys`"
                        "arguement with a set of variables that uniquely identify each row."
                    )
                else:
                    raise CensusApiException(
                        f"Neither the merge nor the concat strategy is viable using row_keys: {row_keys}. "
                        "The supplied keys should uniquely identify every row in the dataset to work. "
                        "If you don't need all the variables, it is always safer to "
                        f"download less than {_MAX_VARIABLES_PER_DOWNLOAD} variables. "
                    )

        # Concat strategy is as safe as it will ever be. We hope the server
        # side did not reorder the results across queries.
        logger.info(
            "Using the concat strategy, which is not guaranteed reliable if "
            "the census API returned data for multiple sub-queries of less than "
            "or equal to %d in different row orders. "
            "It is always safest to query no more than %d "
            "variables at a time. Please do so unless you really need them all.",
            _MAX_VARIABLES_PER_DOWNLOAD,
            _MAX_VARIABLES_PER_DOWNLOAD,
        )

        __dw_strategy_metrics["concat"] = __dw_strategy_metrics["concat"] + 1

        df_data = pd.concat(
            [dfs[0]] + [df.drop(geo_key_variables, axis="columns") for df in dfs[1:]],
            axis="columns",
        )

    return df_data


@dataclass
class _ShapefileRoot:
    """A private class to stash the root we will use to cache shapefiles locally."""

    shapefile_root: Optional[Path] = None


__shapefile_root = _ShapefileRoot()
__shapefile_readers: Dict[int, cmap.ShapeReader] = {}


def set_shapefile_path(shapefile_path: Union[Path, None]) -> None:
    """
    Set the path to the directory to cache shapefiles.

    This is where we will cache shapefiles downloaded when
    `with_geometry=True` is passed to :py:func:`~download`.

    Parameters
    ----------
    shapefile_path
        The path to use for caching shapefiles.
    """
    __shapefile_root.shapefile_root = shapefile_path


def get_shapefile_path() -> Union[Path, None]:
    """
    Get the path to the directory to cache shapefiles.

    This is where we will cache shapefiles downloaded when
    `with_geometry=True` is passed to :py:func:`~download`.

    Returns
    -------
        The path to use for caching shapefiles.
    """
    return __shapefile_root.shapefile_root


def __shapefile_reader(year: int):
    reader = __shapefile_readers.get(year, None)

    if reader is None:
        reader = cmap.ShapeReader(
            __shapefile_root.shapefile_root,
            year,
        )

        __shapefile_readers[year] = reader

    return reader


def _congressional_district_from_year(year: int) -> str:
    """Construct the short form of the congressional district used in a given year."""
    # See the files in https://www2.census.gov/geo/tiger/GENZ2020/shp/
    # and similar. The interesting ones are of the form
    #
    # cb_20YY_us_cdCCC_500k.zip
    #
    # where YY is the year and CCC is the congressional district
    # used.
    #
    # The mappings are not exactly at two year intervals as we would expect.
    if year == 2020 or year == 2021:
        # For some reason they did not update to cd117 for these years. Pandemic?
        return "cd116"

    # Regular year rule.
    congress = 104 + (year - 1994) // 2
    return f"cd{congress}"


_GEO_QUERY_FROM_DATA_QUERY_INNER_GEO: Dict[
    str,
    Union[
        Tuple[Optional[str], str, List[str], List[str]],
        Callable[[str], Tuple[Optional[str], str, List[str], List[str]]],
    ],
] = {
    # innermost geo: ( shapefile_scope, shapefile_geo_name, df_on, gdf_on )
    "region": ("us", "region", ["REGION"], ["REGIONCE"]),
    "division": ("us", "division", ["DIVISION"], ["DIVISIONCE"]),
    "combined statistical area": (
        "us",
        "csa",
        ["COMBINED_STATISTICAL_AREA"],
        ["CSAFP"],
    ),
    "metropolitan statistical area/micropolitan statistical area": (
        "us",
        "cbsa",
        ["METROPOLITAN_STATISTICAL_AREA_MICROPOLITAN_STATISTICAL_AREA"],
        ["CBSAFP"],
    ),
    "state": ("us", "state", ["STATE"], ["STATEFP"]),
    "consolidated city": (
        "us",
        "concity",
        ["STATE", "CONSOLIDATED_CITY"],
        ["STATEFP", "CONCTYFP"],
    ),
    "county": ("us", "county", ["STATE", "COUNTY"], ["STATEFP", "COUNTYFP"]),
    "public use microdata area": lambda year: (
        "us",
        "puma10" if year < 2020 else "puma20",
        ["STATE", "PUBLIC_USE_MICRODATA_AREA"],
        ["STATEFP", "PUMACE"] if year < 2020 else ["STATEFP20", "PUMACE20"],
    ),
    "congressional district": lambda year: (
        "us",
        _congressional_district_from_year(year),
        ["STATE", "CONGRESSIONAL_DISTRICT"],
        ["STATEFP", f"{_congressional_district_from_year(year).upper()}FP"],
    ),
    # For these, the shapefiles are at the state level, so `None`
    # indicates that we have to fill it in based on the geometry
    # being queried.
    "county subdivision": (
        None,
        "cousub",
        ["STATE", "COUNTY_SUBDIVISION"],
        ["STATEFP", "COUSUBFP"],
    ),
    "place": (None, "place", ["STATE", "PLACE"], ["STATEFP", "PLACEFP"]),
    "tract": (
        None,
        "tract",
        ["STATE", "COUNTY", "TRACT"],
        ["STATEFP", "COUNTYFP", "TRACTCE"],
    ),
    "block group": (
        None,
        "bg",
        ["STATE", "COUNTY", "TRACT", "BLOCK_GROUP"],
        ["STATEFP", "COUNTYFP", "TRACTCE", "BLKGRPCE"],
    ),
    "block": (
        None,
        "tabblock",
        ["STATE", "COUNTY", "TRACT", "BLOCK"],
        ["STATEFP", "COUNTYFP", "TRACTCE", "BLOCKCE"],
    ),
    "school district (unified)": (
        None,
        "unsd",
        ["STATE", "SCHOOL_DISTRICT_UNIFIED"],
        ["STATEFP", "UNSDLEA"],
    ),
}
"""
Helper map for the _with_geometry case.

A map from the innermost level of a geometry specification
to the arguments we need to pass to `get_cb_shapefile`
to get the right shapefile for the geography and the columns
we need to join the data and shapefile on.

Most values are tuples, but a few are functions of the year.
If a value is callable, then we call it with the year as an
argument to get the final value. This is necessary for e.g.
congressional districts, which have names that change every
two years.

We should add everything in
https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html
to this map.
"""


def _geo_query_from_data_query_inner_geo(
    year: int, geo_level: str
) -> Tuple[Optional[str], str, List[str], List[str]]:
    """
    Map lookup and call the value if it is callable to produce the result.

    Parameters
    ----------
    year
        The year to pass to callable values.
    geo_level
        The key to look up.

    Returns
    -------
        A tuple oof results.
    """
    if geo_level not in _GEO_QUERY_FROM_DATA_QUERY_INNER_GEO:
        raise CensusApiException(
            "The with_geometry=True flag is only allowed if the "
            f"geometry for the data to be loaded ('{geo_level}') is one of "
            f"{list(_GEO_QUERY_FROM_DATA_QUERY_INNER_GEO.keys())}."
        )

    tuple_or_func = _GEO_QUERY_FROM_DATA_QUERY_INNER_GEO[geo_level]

    if callable(tuple_or_func):
        return tuple_or_func(year)

    return tuple_or_func


def _geo_query_from_data_query_inner_geo_items(
    year: int,
) -> Generator[Tuple[Optional[str], str, List[str], List[str]], None, None]:
    """Generate the items in `GEO_QUERY_FROM_DATA_QUERY_INNER_GEO`."""
    for geo_level in _GEO_QUERY_FROM_DATA_QUERY_INNER_GEO:
        yield geo_level, _geo_query_from_data_query_inner_geo(year, geo_level)


def _add_geography(
    df_data: pd.DataFrame,
    year: Optional[VintageType],
    shapefile_scope: str,
    geo_level: str,
) -> Union[pd.DataFrame, gpd.GeoDataFrame]:
    """
    Add geography to data.

    Parameters
    ----------
    df_data
        The data we downloaded from the census API
    year
        The year for which to fetch geometries. We need this
        because they change over time. If `None`, look for a
        `'YEAR'` column in `df_data` and possibly add different
        geometries for different years as needed.
    shapefile_scope
        The scope of the shapefile. This is typically either a state
        such as `NJ` or the string `"us"`.
    geo_level
        The geography level we want to add.

    Returns
    -------
        A GeoDataFrame with the original data and an
        added geometry column for each row.
    """
    (
        query_shapefile_scope,
        shapefile_geo_level,
        df_on,
        gdf_on,
    ) = _geo_query_from_data_query_inner_geo(year, geo_level)

    # If the query spec has a hard-coded value then we use it.
    if query_shapefile_scope is not None:
        shapefile_scope = query_shapefile_scope

    def individual_shapefile(sub_scope: str, query_year: int) -> gpd.GeoDataFrame:
        """Read the relevant shapefile and add a YEAR column to it."""
        try:
            gdf = __shapefile_reader(query_year).try_cb_tiger_shapefile(
                sub_scope, shapefile_geo_level
            )
            gdf["YEAR"] = query_year
            return gdf
        except cmap.MapException:
            # If there are some years where we can't find a shapefile,
            # skip over it and those rows will not have geometry in the
            # final result.
            logger.info(
                "Unable to load shapefile for scope %s for year %d",
                sub_scope,
                query_year,
            )
            return gpd.GeoDataFrame()

    # If there is a single defined year then we can load the single
    # shapefile. If not, then we have to load multiple shapefiles,
    # one per year, and concatenate them.
    #
    # Whether there is a single or multiple years, there could also
    # me mutliple scopes, e.g. states, for which we have to download
    # shapefiles. If so, by the time we get here, they are encoded in
    # one string with comma separators.
    if isinstance(year, int):
        gdf_shapefile = pd.concat(
            individual_shapefile(sub_scope, year)
            for sub_scope in shapefile_scope.split(",")
        )
        merge_gdf_on = gdf_on
    else:
        gdf_shapefile = pd.concat(
            individual_shapefile(sub_scope, unique_year)
            for unique_year in df_data["YEAR"].unique()
            for sub_scope in shapefile_scope.split(",")
        )

        merge_gdf_on = ["YEAR"] + gdf_on
        df_on = ["YEAR"] + df_on

    if len(gdf_shapefile.index) == 0:
        # None of the years matched, so we add None for geometry to all.
        gdf = gpd.GeoDataFrame(df_data, copy=True)
        gdf.set_geometry([None for _ in gdf.index], inplace=True)
        return gdf

    if "TRACT" in df_data.columns:
        df_data["TRACT"] = df_data["TRACT"].str.ljust(
            6, "0"
        )  # Pre 2010 data has inconsistencies in TRACT string length

    gdf_data = gdf_shapefile[merge_gdf_on + ["geometry"]].merge(
        df_data, how="right", left_on=merge_gdf_on, right_on=df_on
    )

    # Get the columns we want in a reasonable order matching
    # how they are in the data, with geometry at the end.
    gdf_data = gdf_data[list(df_data.columns) + ["geometry"]]

    # Rearrange columns so geometry is at the end.
    gdf_data = gdf_data[
        [col for col in gdf_data.columns if col != "geometry"] + ["geometry"]
    ]

    return gdf_data


def infer_geo_level(year: Optional[int], df_data: pd.DataFrame) -> str:
    """
    Infer the geography level based on columns names.

    Parameters
    ----------
    year
        The vintage of the data. `None` to infer from the data.
    df_data
        A dataframe of variables with one or more columns that
        can be used to infer what geometry level the rows represent.

        For example, if the column `"STATE"` exists, we could infer that
        the data in on a state by state basis. But if there are
        columns for both `"STATE"` and `"COUNTY"`, the data is probably
        at the county level.

        If, on the other hand, there is a `"COUNTY" column but not a
        `"STATE"` column, then there is some ambiguity. The data
        probably corresponds to counties, but the same county ID can
        exist in multiple states, so we will raise a
        :py:class:`~CensusApiException` with an error message expalining
        the situation.

        If there is no match, we will also raise an exception. Again we
        do this, rather than for example, returning `None`, so that we
        can provide an informative error message about the likely cause
        and what to do about it.

        This function is not often called directly, but rather from
        :py:func:`~add_inferred_geography`, which infers the geography
        level and then adds a `geometry` column containing the appropriate
        geography for each row.

    Returns
    -------
        The name of the geography level.
    """
    match_key = None
    match_on_len = 0
    partial_match_keys = []

    for k, (_, _, df_on, _) in _geo_query_from_data_query_inner_geo_items(year):
        if all(col in df_data.columns for col in df_on):
            # Full match. We want the longest full match
            # we find.
            if match_key is None or len(df_on) > match_on_len:
                match_key = k
        elif df_on[-1] in df_data.columns:
            # Partial match. This could result in us
            # not getting what we expect. Like if we
            # have STATE and TRACT, but not COUNTY, we will
            # get a partial match on [STATE, COUNTY. TRACT]
            # and a full match on [STATE]. We probably did
            # not mean to match [STATE], we just lost the
            # COUNTY column somewhere along the way.
            partial_match_keys.append(k)

    if match_key is None:
        raise CensusApiException(
            f"Unable to infer geometry. Was not able to locate any of the "
            "known sets of columns "
            f"{tuple(df_on for _, (_, _, df_on, _) in _geo_query_from_data_query_inner_geo_items(year))} "
            f"in the columns {list(df_data.columns)}."
        )

    if partial_match_keys:
        raise CensusApiException(
            f"Unable to infer geometry. Geometry matched {match_key} on columns "
            f"{_geo_query_from_data_query_inner_geo(year, match_key)[2]} "
            "but also partially matched one or more candidates "
            f"{tuple(_geo_query_from_data_query_inner_geo(year, k)[2] for k in partial_match_keys)}. "
            "Partial matches are usually unintended. Either add columns to allow a "
            "full match or rename some columns to prevent the undesired partial match."
        )

    return match_key


def add_inferred_geography(
    df_data: pd.DataFrame, year: Optional[int] = None
) -> gpd.GeoDataFrame:
    """
    Infer the geography level of the given dataframe.

    Add geometry to each row for the inferred level.

    See Also
    --------
        :py:ref:`~infer_geo_level` for more on how inference is done.

    Parameters
    ----------
    df_data
        A dataframe of variables with one or more columns that
        can be used to infer what geometry level the rows represent.
    year
        The year for which to fetch geometries. We need this
        because they change over time. If `None`, look for a
        `'YEAR'` column in `df_data` and possibly add different
        geometries for different years as needed.

    Returns
    -------
        A geo data frame containing the original data augmented with
        the appropriate geometry for each row.
    """
    if year is None:
        # We'll try to get the year out of the data.
        if "YEAR" not in df_data.columns:
            raise ValueError(
                "If year is None then there must be a `YEAR` column in the data."
            )

        return gpd.GeoDataFrame(
            df_data.groupby("YEAR", group_keys=False)
            .apply(lambda df_group: add_inferred_geography(df_group, df_group.name))
            .reset_index(drop=True)
        )

    geo_level = infer_geo_level(year, df_data)

    (
        shapefile_scope,
        _,
        shapefile_scope_columns,
        _,
    ) = _geo_query_from_data_query_inner_geo(year, geo_level)

    if shapefile_scope is not None:
        # The scope is the same across the board.
        gdf = _add_geography(df_data, year, shapefile_scope, geo_level)
        return gdf

    # We have to group by different values of the shapefile
    # scope from the appropriate column and add the right
    # geography to each group.
    shapefile_scope_column = shapefile_scope_columns[0]

    df_with_geo = (
        df_data.groupby(shapefile_scope_column, group_keys=False)
        .apply(lambda g: _add_geography(g, year, g.name, geo_level))
        .reset_index(drop=True)
    )

    gdf = gpd.GeoDataFrame(df_with_geo)

    return gdf


def download(
    dataset: str,
    vintage: VintageType,
    download_variables: Optional[Union[str, Iterable[str]]] = None,
    *,
    group: Optional[Union[str, Iterable[str]]] = None,
    leaves_of_group: Optional[Union[str, Iterable[str]]] = None,
    set_to_nan: Union[bool, Iterable[int]] = True,
    skip_annotations: bool = True,
    with_geometry: bool = False,
    remove_water: bool = False,
    api_key: Optional[str] = None,
    variable_cache: Optional["VariableCache"] = None,
    row_keys: Optional[Union[str, Iterable[str]]] = None,
    **kwargs: cgeo.InSpecType,
) -> Union[pd.DataFrame, gpd.GeoDataFrame]:
    """
    Download data from the US Census API.

    This is the main API for downloading US Census data with the
    `censusdis` package. There are many examples of how to use
    this in the demo notebooks provided with the package at
    https://github.com/vengroff/censusdis/tree/main/notebooks.

    *A note on variables and groups*: there are multiple ways to specify the
    variables you want to download, either individually in `download_variables`,
    by one or more groups in `group`, and by the leaves of one or more groups
    in `leaves_of_group`. Note that these three sources af variables are
    deduplicated, so you will only get one column for a variable no matter
    how many times it is specified.

    *Specifying census geographies*: censusdis provides access to many
    census datasets, each of which can be retrieved at a particular set of
    geographic grains. To accomodate this, `download()` takes a set
    of kwargs to define the geographic level of the returned data. You can check
    which geographies are available for a particular dataset with the
    `geographies()`.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`. There are
        symbolic names for datasets, like `ACS5` for `"acs/acs5"
        in :py:module:`censusdis.datasets`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.
    download_variables
        The census variables to download, for example `["NAME", "B01001_001E"]`.
    group
        One or more groups (as defined by the U.S. Census for the data set)
        whose variable values should be downloaded. These are in addition to
        any specified in `download_variables`.
    leaves_of_group
        One or more groups (as defined by the U.S. Census for the data set)
        whose leaf variable values should be downloaded.These are in addition to
        any specified in `download_variables` or `group`. See
        :py:meth:`VariableCache.group_leaves` for more details on the semantics of
        leaves vs. non-leaf group variables.
    set_to_nan
        A list of values that should be set to NaN. Normally these are special
        values that the U.S. Census API sometimes returns. If `True`, then all
        values in :py:ref:`censusdis.values.ALL_SPECIAL_VALUES` will be replaced.
        If `False`, no replacements will be made.
    skip_annotations
        If `True` try to filter out `group` or `leaves_of_group` variables that are
        annotations rather than actual values. See :py:meth:`VariableCache.group_variables`
        for more details. Variable names passed in `download_variables` are not
        affected by this flag.
    with_geometry
        If `True` a :py:class:`gpd.GeoDataFrame` will be returned and each row
        will have a geometry that is a cartographic boundary suitable for platting
        a map. See https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.2020.html
        for details of the shapefiles that will be downloaded on your behalf to
        generate these boundaries.
    remove_water
        If `True` and if with_geometry=True, will query TIGER for AREAWATER shapefiles and
        remove water areas from returned geometry.
    api_key
        An optional API key. If you don't have or don't use a key, the number
        of calls you can make will be limited to 500 per day.
    variable_cache
        A cache of metadata about variables.
    row_keys
        An optional set of identifier keys to help merge together requests for more than the census API limit of
        50 variables per query. These keys are useful for census datasets such as the Current Population Survey
        where the geographic identifiers do not uniquely identify each row.
    kwargs
        A specification of the geometry that we want data for. For example,
        `state = "*", county = "*"` will download county-level data for
        the entire US.

    Returns
    -------
        A :py:class:`~pd.DataFrame` or `~gpd.GeoDataFrame` containing the requested US Census data.
    """
    if variable_cache is None:
        variable_cache = variables

    # Ensure list operations work
    if row_keys:
        row_keys = list(row_keys)

    # The side effect here is to prime the cache.
    cgeo.geo_path_snake_specs(dataset, vintage)

    if set_to_nan is True:
        set_to_nan = ALL_SPECIAL_VALUES

    # In case they came to us in py format, as kwargs often do.
    kwargs = {
        cgeo.path_component_from_snake(dataset, vintage, k): v
        for k, v in kwargs.items()
    }

    # Parse out the download variables
    download_variables = _parse_download_variables(
        dataset,
        vintage,
        download_variables=download_variables,
        group=group,
        leaves_of_group=leaves_of_group,
        skip_annotations=skip_annotations,
        variable_cache=variable_cache,
    )

    if len(download_variables) <= _MAX_VARIABLES_PER_DOWNLOAD and row_keys:
        warnings.warn(
            "\n The row_keys arguement is intended to be used only when the number of requested"
            "\n variables exceeds the Census defined limit of 50"
            "\n The supplied value(s) will be ignored",
            UserWarning,
        )
    # Special case if we are trying to get too many fields.
    if len(download_variables) > _MAX_VARIABLES_PER_DOWNLOAD:
        return _download_multiple(
            dataset,
            vintage,
            download_variables,
            key=api_key,
            census_variables=variable_cache,
            with_geometry=with_geometry,
            row_keys=row_keys,
            **kwargs,
        )

    # Prefetch all the types before we load the data.
    # That way we fail fast if a field is not known.
    _prefetch_variable_types(dataset, vintage, download_variables, variable_cache)
    # Also check that the row_keys, if supplied, are present in the dataset
    if row_keys:
        _prefetch_variable_types(dataset, vintage, row_keys, variable_cache)

    # If we were given a list, join it together into
    # a comma-separated string.
    string_kwargs = {k: _gf2s(v) for k, v in kwargs.items()}

    return _download_remote(
        dataset,
        vintage,
        download_variables=download_variables,
        set_to_nan=set_to_nan,
        with_geometry=with_geometry,
        remove_water=remove_water,
        api_key=api_key,
        variable_cache=variable_cache,
        **string_kwargs,
    )


def _download_remote(
    dataset: str,
    vintage: VintageType,
    *,
    download_variables: List[str],
    set_to_nan: Union[bool, Iterable[float]] = True,
    with_geometry: bool,
    remove_water: bool,
    api_key: Optional[str],
    variable_cache: "VariableCache",
    **kwargs,
) -> Union[pd.DataFrame, gpd.GeoDataFrame]:
    """
    Make the actual remote call to download the data.

    This is the final step after we have parsed out and
    validated the variables and geometry.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.
    download_variables
        The census variables to download, for example `["NAME", "B01001_001E"]`.
    set_to_nan
        A list of values that should be set to NaN. Normally these are special
        values that the U.S. Census API sometimes returns. If `True`, then all
        values in :py:ref:`censusdis.values.ALL_SPECIAL_VALUES` will be replaced.
        If `False`, no replacements will be made.
    with_geometry
        If `True` a :py:class:`gpd.GeoDataFrame` will be returned and each row
        will have a geometry that is a cartographic boundary suitable for platting
        a map. See https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.2020.html
        for details of the shapefiles that will be downloaded on your behalf to
        generate these boundaries.
    api_key
        An optional API key. If you don't have or don't use a key, the number
        of calls you can make will be limited.
    variable_cache
        A cache of metadata about variables.
    kwargs
        A specification of the geometry that we want data for.

    Returns
    -------
        The downloaded variables, with or without added geometry, as
        either a `pd.DataFrame` or `gpd.GeoDataFrame`.
    """
    url, params, bound_path = census_table_url(
        dataset, vintage, download_variables, api_key=api_key, **kwargs
    )
    df_data = data_from_url(url, params)

    # Coerce the types based on metadata about the variables.
    _coerce_downloaded_variable_types(
        dataset, vintage, download_variables, df_data, variable_cache
    )

    download_variables_upper = [dv.upper() for dv in download_variables]

    # Put the geo fields (STATE, COUNTY, etc...) that came back up front.
    df_data = df_data[
        [col for col in df_data.columns if col not in download_variables_upper]
        + download_variables_upper
    ]

    # NaN out as requested.
    if set_to_nan is True:
        set_to_nan = ALL_SPECIAL_VALUES
    if set_to_nan:
        df_data = df_data.replace(list(set_to_nan), np.nan)

    if with_geometry:
        # We need to get the geometry and merge it in.
        geo_level = bound_path.path_spec.path[-1]
        shapefile_scope = bound_path.bindings[bound_path.path_spec.path[0]]

        gdf_data = _add_geography(df_data, vintage, shapefile_scope, geo_level)

        if remove_water:
            gdf_data = clip_water(gdf_data, vintage)

        return gdf_data

    return df_data


def _coerce_downloaded_variable_types(
    dataset: str,
    vintage: VintageType,
    download_variables: List[str],
    df_data: pd.DataFrame,
    variable_cache: "VariableCache",
) -> None:
    """
    Coerce the type of each returned variable (column) in a data frame.

    We look up the type in the metadata in `variable_cache`.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.
    download_variables
        The census variables to download, for example `["NAME", "B01001_001E"]`.
    df_data
        The data that came back in JSON form from the census API.
    variable_cache
        A cache of metadata about variables.
    """
    for variable in download_variables:
        # predicateType does not exist in some older data sets like acs/acs3
        # So in that case we just go with what we got in the JSON. But if we
        # have it try to set the type.
        if "predicateType" in variable_cache.get(dataset, vintage, variable):
            field_type = variable_cache.get(dataset, vintage, variable)["predicateType"]

            if field_type == "int":
                if df_data[variable].isnull().any():
                    # Some Census data sets put in null in int fields.
                    # We have to go with a float to make this a NaN.
                    # Int has no representation for NaN or None.
                    df_data[variable] = df_data[variable].astype(float, errors="ignore")
                else:
                    try:
                        df_data[variable] = df_data[variable].astype(int)
                    except ValueError:
                        # Sometimes census metadata says int, but they
                        # put in float values anyway, so fall back on
                        # trying to get them as floats.
                        df_data[variable] = df_data[variable].astype(
                            float, errors="ignore"
                        )
                    except OverflowError:
                        # Some long IDs are actually better handled as strings.
                        df_data[variable] = df_data[variable].astype(str)
            elif field_type == "float":
                df_data[variable] = df_data[variable].astype(float)
            elif field_type == "string":
                pass
            else:
                # Leave it as an object?
                pass


def _prefetch_variable_types(
    dataset: str,
    vintage: VintageType,
    download_variables: List[str],
    variable_cache: "VariableCache",
) -> None:
    """
    Prefetch the types of all the variables we are going to try to download.

    This enables us to fail fast and have a better error message about the
    root cause of the issue than if we just blindly put in the variable names
    in the census API request and wait for it to fail.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.

    download_variables
        The census variables to download, for example `["NAME", "B01001_001E"]`.
    variable_cache
        A cache of metadata about variables.
    """
    for variable in download_variables:
        try:
            variable_cache.get(dataset, vintage, variable)
        except Exception as exc:
            census_url = CensusApiVariableSource.url(
                dataset, vintage, variable, response_format="html"
            )
            census_variables_url = CensusApiVariableSource.variables_url(
                dataset, vintage, response_format="html"
            )

            raise CensusApiException(
                f"Unable to get metadata on the variable {variable} from the "
                f"dataset {dataset} for year {vintage} from the census API. "
                f"Check the census URL for the variable ({census_url}) to ensure it exists. "
                f"If not found, check {census_variables_url} for all variables in the dataset."
            ) from exc


def _parse_download_variables(
    dataset: str,
    vintage: VintageType,
    *,
    download_variables: Optional[Union[str, Iterable[str]]] = None,
    group: Optional[Union[str, Iterable[str]]] = None,
    leaves_of_group: Optional[Union[str, Iterable[str]]] = None,
    skip_annotations: bool = True,
    variable_cache: Optional["VariableCache"] = None,
) -> List[str]:
    """
    Parse out the full set of download variables.

    These may be encoded in `download_variables`, `group`, and/or `leaves_of_group`.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.
    download_variables
        The census variables to download, for example `["NAME", "B01001_001E"]`.
    group
        One or more groups (as defined by the U.S. Census for the data set)
        whose variable values should be downloaded. These are in addition to
        any specified in `download_variables`.
    leaves_of_group
        One or more groups (as defined by the U.S. Census for the data set)
        whose leaf variable values should be downloaded.These are in addition to
        any specified in `download_variables` or `group`. See
        :py:meth:`VariableCache.group_leaves` for more details on the semantics of
        leaves vs. non-leaf group variables.
    skip_annotations
        If `True` try to filter out `group` or `leaves_of_group` variables that are
        annotations rather than actual values. See :py:meth:`VariableCache.group_variables`
        for more details. Variable names passed in `download_variables` are not
        affected by this flag.
    variable_cache
        A cache of metadata about variables.

    Returns
    -------
        The fully expanded list of variables to download.
    """
    # Turn the variables we were given into a list if they are not already.
    if download_variables is None:
        download_variables = []
    elif isinstance(download_variables, str):
        download_variables = [download_variables]
    elif not isinstance(download_variables, list):
        download_variables = list(download_variables)

    if group is None:
        group = []
    elif isinstance(group, str):
        group = [group]

    if leaves_of_group is None:
        leaves_of_group = []
    elif isinstance(leaves_of_group, str):
        leaves_of_group = [leaves_of_group]

    # Add group variables and leaves as appropriate.
    group_variables: List[str] = []
    for group_name in group:
        group_variables = group_variables + variable_cache.group_variables(
            dataset, vintage, group_name, skip_annotations=skip_annotations
        )
    group_leaf_variables: List[str] = []
    for group_name in leaves_of_group:
        group_leaf_variables = group_leaf_variables + variable_cache.group_leaves(
            dataset, vintage, group_name, skip_annotations=skip_annotations
        )

    # Concatenate them all.
    download_variables = download_variables + group_variables + group_leaf_variables

    # Dedup and maintain order.
    download_variables = list(dict.fromkeys(download_variables))

    return download_variables


def census_table_url(
    dataset: str,
    vintage: VintageType,
    download_variables: Iterable[str],
    *,
    api_key: Optional[str] = None,
    **kwargs: cgeo.InSpecType,
) -> Tuple[str, Mapping[str, str], cgeo.BoundGeographyPath]:
    """
    Construct the URL to download data from the U.S. Census API.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.
    download_variables
        The census variables to download, for example `["NAME", "B01001_001E"]`.
    api_key
        An optional API key. If you don't have or don't use a key, the number
        of calls you can make will be limited.
    kwargs
        A specification of the geometry that we want data for.

    Returns
    -------
        The URL, parameters and bound path.

    """
    bound_path = _bind_path_if_possible(dataset, vintage, **kwargs)

    query_spec = cgeo.CensusGeographyQuerySpec(
        dataset, vintage, list(download_variables), bound_path, api_key=api_key
    )

    url, params = query_spec.table_url()

    return url, params, bound_path


def _bind_path_if_possible(dataset, vintage, **kwargs):
    """
    Bind the path if possible.

    If not, raise an exception with enough info to fix it.
    """
    bound_path = cgeo.PathSpec.partial_prefix_match(dataset, vintage, **kwargs)
    if bound_path is None:
        raise CensusApiException(
            f"Unable to match the geography specification {kwargs}.\n"
            f"Supported geographies for dataset='{dataset}' in year={vintage} are:\n"
            + "\n".join(
                f"{path_spec}"
                for path_spec in cgeo.geo_path_snake_specs(dataset, vintage).values()
            )
        )
    return bound_path


def geography_names(
    dataset: str,
    vintage: VintageType,
    **kwargs: cgeo.InSpecType,
) -> pd.DataFrame:
    """
    Get the name of a specific geography.

    The arguments are a subset of those to :py:func:`~download`. This
    function is designed to make it easy to fetch the name of a geography
    when we know the FIPS code but want a human-readable name or label for
    display.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `censusdis.datasets.ACS5`.
    vintage
        The vintage to download data for. For example, `2020`.
    kwargs
        A specification of the geometry that we want data for. For example,
        `state = "34", county = "017"` will download the name of Hudson County,
        New Jersey.

    Returns
    -------
        A dataframe with columns specifying the geography and one for the name.
        All column names will be in ALL CAPS.
    """
    df = download(dataset, vintage, ["NAME"], **kwargs)

    return df


def geographies(dataset: str, vintage: VintageType) -> List[List[str]]:
    """
    Determine what geographies are supported for a dataset and vintage.

    This utility gives us a list of the different geography
    keywords we can use in calls to :py:func:`download` with
    for the given dataset and vintage.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.

    Returns
    -------
        A list of lists of geography keywords. Each element
        of the outer list is a list of keywords that can be
        used together.
    """
    return list(cgeo.geo_path_snake_specs(dataset, vintage).values())


variables = VariableCache()


def _identify_counties(gdf_geo: gpd.GeoDataFrame, year: int):
    """
    Takesa geodataframe and identify which US counties the supplied geography overlaps.

    Parameters
    ----------
    gdf_geo
        A GeoDataFrame containing polygons within the United States
    year
        The year for which to fetch geometries. We need this
        because they change over time.

    Returns
    -------
        A list of five digit county FIPS codes.
    """
    # Some dataframes will contain the county column already
    if "STATE" in gdf_geo and "COUNTY" in gdf_geo:
        fips_codes = gdf_geo["STATE"] + gdf_geo["COUNTY"]

        return fips_codes.unique().tolist()
    # Otherwise, we load all the US counties and perform an overlap operation
    else:
        reader = __shapefile_readers.get(year)
        us_counties = reader.read_cb_shapefile("us", "county")
        us_counties["FIPS"] = us_counties["STATEFP"] + us_counties["COUNTYFP"]

        county_overlap = us_counties.overlay(gdf_geo, keep_geom_type=False)
        fips_codes = county_overlap["STATEFP"] + county_overlap["COUNTYFP"]

        return fips_codes.unique().tolist()


def _retrieve_water(county_fips_codes: list[str], year: int):
    """
    Load `AREAWATER` files from tiger for specified counties.

    Parameters
    ----------
    county_FIPS_codes
        A list of five digit county FIPS codes

    Returns
    -------
        A GeoDataFrame containing the census defined water in the supplied counties
    """
    reader = __shapefile_readers.get(year)

    gdf_water = pd.concat(
        reader.read_shapefile(shapefile_scope=county, geography="areawater")
        for county in county_fips_codes
    )
    # Geo pandas has no concat method, so we convert from a pandas df
    gdf_water = gpd.GeoDataFrame(gdf_water)
    return gdf_water


def _water_difference(
    gdf_geo: gpd.GeoDataFrame, gdf_water: gpd.GeoDataFrame, minimum_area_sq_meters: int
):
    """
    Remove water polygons exceeding minimum size from supplied `GeoDataFrame`.

    Parameters
    ----------
    gdf_geo
        A GeoDataFrame containing polygons within the United States
    gdf_water
        A GeoDataFrame containing census AREAWATER polygons
    minimum_area_sq_meters
        The smallest water polygon to be removed, specified in square meters

    Returns
    -------
        A version of gdf_geo with the water areas removed
    """
    # Combining polygons speeds up the overlay operation
    geo_combined_water = gdf_water[
        gdf_water["AWATER"] >= minimum_area_sq_meters
    ].unary_union
    gdf_combined_water = gpd.GeoDataFrame(geometry=[geo_combined_water])
    gdf_combined_water = gdf_combined_water.set_crs(gdf_water.crs)

    return gdf_geo.overlay(
        gdf_combined_water,
        "difference",
        keep_geom_type=False,
    )


def clip_water(
    gdf_geo: gpd.GeoDataFrame,
    year: int,
    minimum_area_sq_meters: int = 10000,
    sliver_threshold=0.01,
):
    """
    Remove water from input `GeoDataFrame`.

    Parameters
    ----------
    gdf_geo
        The GeoDataFrame from which we want to remove water
    year
        The year for which to fetch geometries. We need this
        because they change over time.
    minimum_area_sq_meters
        The minimimum size of a water area to be removed

    Returns
    -------
        A GeoDataFrame with the water areas larger than
        the specified threshold removed.
    """
    counties = _identify_counties(gdf_geo, year)
    gdf_water = _retrieve_water(counties, year)

    gdf_without_water = _water_difference(gdf_geo, gdf_water, minimum_area_sq_meters)

    original_crs = gdf_without_water.crs
    gdf_without_water = drop_slivers_from_gdf(
        gdf_without_water.to_crs(epsg=3857), threshold=sliver_threshold
    ).to_crs(original_crs)

    return gdf_without_water


def _intersecting_geos_kws(
    dataset: str,
    vintage: VintageType,
    containing_geo_kwargs: cgeo.InSpecType,
    **kwargs: cgeo.InSpecType,
) -> cgeo.InSpecType:
    """
    Construct geography keywords for intersecting geographies.

    Parameters
    ----------
    dataset
        The dataset to download from. For example `"acs/acs5"`,
        `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`. There are
        symbolic names for datasets, like `ACS5` for `"acs/acs5"
        in :py:module:`censusdis.datasets`.
    vintage
        The vintage to download data for. For most data sets this is
        an integer year, for example, `2020`. But for
        a timeseries data set, pass the string `'timeseries'`.
    containing_geo_kwargs
        Geographic keywords specifying the containing geography that we are
        looking for intersections with. For example
        `dict(metropolitan_statistical_area_micropolitan_statistical_area="35620")`
        for the New York area CBSA.
    kwargs
        A specification of the geometry that we want data for, limited to those
        geographies that are contained in the geography specified by `containing_geo_kwargs`.
        For example, `state="*", county="*", tract="*"` will specifies county-level data for
        all counties contained in the containing geography.

    Returns
    -------
        A dictionary of geographic keywords suitable for passing to :py:func:`~download`.
    """
    # This is a fast short circuit if the first component
    # is already specified.
    if list(kwargs.values())[0] != "*":
        return kwargs

    # Download the geometry of the outer scope.
    gdf_within = download(
        dataset, vintage, ["NAME"], with_geometry=True, **containing_geo_kwargs
    )

    # See if we can find a matching path spec.
    bound_path = _bind_path_if_possible(dataset, vintage, **kwargs)

    # Get the geography for the outermost level of the match.
    first_binding = list(bound_path.bindings.items())[0]

    containing_geo_kwargs = {first_binding[0]: first_binding[1]}

    gdf_first_binding = download(
        dataset, vintage, ["NAME"], with_geometry=True, **containing_geo_kwargs
    )

    # Which of the first binding geographies intersect
    # the area we want our final geographies to be in.
    gdf_intersects = gdf_first_binding.sjoin(
        gdf_within, lsuffix="FIRST", rsuffix="within"
    )

    col_name = first_binding[0].upper()
    if col_name not in gdf_intersects.columns:
        col_name = f"{col_name}_FIRST"

    intersecting_geographies = list(gdf_intersects[col_name].unique())
    intersecting_geographies = [
        geo[:-6] if geo.endswith("_FIRST") else geo for geo in intersecting_geographies
    ]

    geo = dict(bound_path.bindings)

    geo[first_binding[0]] = intersecting_geographies

    return geo


class ContainedWithin:
    """A representation of a geography that we want to query some other geographies that are contained within."""

    def __init__(self, area_threshold: float = 0.8, **kwargs: cgeo.InSpecType):
        """
        Construct a representation of a geography that we want to query some other geographies contained within.

        Parameters
        ----------
        area_threshold
            What fraction of the area of other geographies must be contained
            in our geography to be included.
        kwargs
            A specification of the geometry that we want data for geometries
            that are contained within. For example,
            `state = "NJ", place = "01960"` will specify the city of Asbury Park, NJ.
        """
        self._area_threshold = area_threshold
        self._containing_kwargs = kwargs

    def __eq__(self, other) -> bool:
        """Are two objects equal."""
        if not isinstance(other, ContainedWithin):
            return False

        return (
            self._area_threshold == other._area_threshold
            and self._containing_kwargs == other._containing_kwargs
        )

    def download(
        self,
        dataset: str,
        vintage: VintageType,
        download_variables: Optional[Union[str, Iterable[str]]] = None,
        *,
        group: Optional[Union[str, Iterable[str]]] = None,
        leaves_of_group: Optional[Union[str, Iterable[str]]] = None,
        set_to_nan: Union[bool, Iterable[int]] = True,
        skip_annotations: bool = True,
        with_geometry: bool = False,
        remove_water: bool = False,
        api_key: Optional[str] = None,
        variable_cache: Optional["VariableCache"] = None,
        row_keys: Optional[Union[str, Iterable[str]]] = None,
        **kwargs: cgeo.InSpecType,
    ) -> Union[pd.DataFrame, gpd.GeoDataFrame]:
        """
        Download data for geographies contained within a containing geography.

        Parameters
        ----------
        dataset
            The dataset to download from. For example `"acs/acs5"`,
            `"dec/pl"`, or `"timeseries/poverty/saipe/schdist"`. There are
            symbolic names for datasets, like `ACS5` for `"acs/acs5"
            in :py:module:`censusdis.datasets`.
        vintage
            The vintage to download data for. For most data sets this is
            an integer year, for example, `2020`. But for
            a timeseries data set, pass the string `'timeseries'`.
        download_variables
            The census variables to download, for example `["NAME", "B01001_001E"]`.
        group
            One or more groups (as defined by the U.S. Census for the data set)
            whose variable values should be downloaded. These are in addition to
            any specified in `download_variables`.
        leaves_of_group
            One or more groups (as defined by the U.S. Census for the data set)
            whose leaf variable values should be downloaded.These are in addition to
            any specified in `download_variables` or `group`. See
            :py:meth:`VariableCache.group_leaves` for more details on the semantics of
            leaves vs. non-leaf group variables.
        set_to_nan
            A list of values that should be set to NaN. Normally these are special
            values that the U.S. Census API sometimes returns. If `True`, then all
            values in :py:ref:`censusdis.values.ALL_SPECIAL_VALUES` will be replaced.
            If `False`, no replacements will be made.
        skip_annotations
            If `True` try to filter out `group` or `leaves_of_group` variables that are
            annotations rather than actual values. See :py:meth:`VariableCache.group_variables`
            for more details. Variable names passed in `download_variables` are not
            affected by this flag.
        with_geometry
            If `True` a :py:class:`gpd.GeoDataFrame` will be returned and each row
            will have a geometry that is a cartographic boundary suitable for platting
            a map. See https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.2020.html
            for details of the shapefiles that will be downloaded on your behalf to
            generate these boundaries.
        remove_water
            If `True` and if with_geometry=True, will query TIGER for AREAWATER shapefiles and
            remove water areas from returned geometry.
        api_key
            An optional API key. If you don't have or don't use a key, the number
            of calls you can make will be limited to 500 per day.
        variable_cache
            A cache of metadata about variables.
        row_keys
            An optional set of identifier keys to help merge together requests for more than the census API limit of
            50 variables per query. These keys are useful for census datasets such as the Current Population Survey
            where the geographic identifiers do not uniquely identify each row.
        kwargs
            A specification of the geometry that we want data for. For example,
            `state = "*", county = "*"` will download county-level data for
            the entire US.

        Returns
        -------
            A :py:class:`~pd.DataFrame` or `~gpd.GeoDataFrame` containing the requested US Census data.
        """
        geos_kwargs = _intersecting_geos_kws(
            dataset, vintage, self._containing_kwargs, **kwargs
        )

        gdf = download(
            dataset,
            vintage,
            download_variables,
            group=group,
            leaves_of_group=leaves_of_group,
            set_to_nan=set_to_nan,
            skip_annotations=skip_annotations,
            with_geometry=True,
            remove_water=remove_water,
            api_key=api_key,
            variable_cache=variable_cache,
            row_keys=row_keys,
            **geos_kwargs,
        )

        # See which of these geometries are mostly contained by
        # the geography we want to be within.

        gdf_container = download(
            dataset, vintage, ["NAME"], with_geometry=True, **self._containing_kwargs
        ).drop("NAME", axis="columns")

        gdf_contained = cmap.sjoin_mostly_contains(
            gdf_container, gdf, area_threshold=self._area_threshold
        )

        # Drop all the large container columns we don't need.
        gdf_contained = gdf_contained[
            [col for col in gdf_contained.columns if not col.endswith("_large")]
        ].reset_index(drop=True)

        # Drop the "_small" suffix.

        gdf_contained.rename(
            lambda col: col[:-6] if col.endswith("_small") else col,
            axis="columns",
            inplace=True,
        )

        if with_geometry:
            # Keep the columns from the larger result.
            return gdf_contained[
                [
                    col
                    for col in gdf_container.columns
                    if col in gdf_contained.columns and col != "geometry"
                ]
                + [
                    col
                    for col in gdf_contained.columns
                    if col not in gdf_container.columns or col == "geometry"
                ]
            ]
        else:
            # Drop the geometry and return a `pd.DataFrame`
            return pd.DataFrame(
                gdf_contained[
                    [
                        col
                        for col in gdf_container.columns
                        if col in gdf_contained.columns and col != "geometry"
                    ]
                    + [
                        col
                        for col in gdf_contained.columns
                        if col not in gdf_container.columns and col != "geometry"
                    ]
                ]
            )


def contained_within(
    area_threshold: float = 0.8, **kwargs: cgeo.InSpecType
) -> ContainedWithin:
    """
    Construct a representation of a geography that we want to query some other geographies contained within.

    Parameters
    ----------
    area_threshold
        What fraction of the area of other geographies must be contained
        in our geography to be included.
    kwargs
        A specification of the geometry that we want data for geometries
        that are contained within. For example,
        `state = "NJ", place = "01960"` will specify the city of Asbury Park, NJ.
    """
    return ContainedWithin(area_threshold=area_threshold, **kwargs)
