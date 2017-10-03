# coding: utf-8

# from __future__ import division
import pandas as pd
import os
from os.path import join, abspath, normpath, dirname, split
import numpy as np
from util.utils import getParentDir, test_function, rename_cols
import json

def add_datetime(df, year='year', month='month'):
    df['datetime'] = pd.to_datetime(df[year].astype(str) + '-' +
                                    df[month].astype(str),
                                    format='%Y-%m')

def add_quarter(df, year='year', month='month'):
    add_datetime(df, year, month)
    df['quarter'] = df['datetime'].dt.quarter


def facility_emission_gen(eia_facility, epa, state_fuel_cat,
                          custom_fuel_cat, export_state_cats=False):
    """
    Use EIA and EPA data to compile emissions, generation and fuel consumption
    reported by facilities into emissions intensity and generation by fuel
    category. Only facilities from the region of interest should be passed to
    this function.

    inputs:
        eia_facility: (dataframe) monthly generation and fuel consumption as
        reported by facilities to EIA
        epa: (dataframe) monthly co2 emissions and gross generation as reported
            by facilities to EPA
        state_fuel_cat (dict): match of state-level fuel categories to facility
            level categories
        custom_fuel_cat (dict): match of custom fuel categories for final
            results to the state-level categories
        export_state_cats (boolean): If co2 and gen should be exported at the
            state category level

    output:
        co2: total adjusted co2 emissions
        gen_fuels: generation and fuel consumption
    """
    # Make column names consistent
    print('Renaming columns')
    rename_cols(eia_facility)
    rename_cols(epa)
    print('Grouping facilities')
    eia_grouped = group_facility_data(eia_facility)

    print('Adjusting EPA emissions')
    epa_adj = adjust_epa_emissions(epa, eia_grouped)

    # I need to return co2 (by facility, or just per month?) and gen/fuels by
    # fuel type.

    print('Caculating CO2')
    co2 = facility_co2(epa_adj, eia_grouped)
    co2 = co2.loc[:, ['year', 'month', 'plant id', 'final co2 (kg)']]

    print('Gen/fuels to state categories')
    gen_fuels_state = group_fuel_cats(eia_facility, state_fuel_cat)
    if export_state_cats:
        return co2, gen_fuels_state
    else:
        print('Gen/fuels to custom categories')
        gen_fuels_custom = group_fuel_cats(gen_fuels_state,
                                           custom_fuel_cat,
                                           fuel_col='type',
                                           new_col='fuel category')
        return co2, gen_fuels_custom

def group_facility_data(eia):
    """
    Group facility co2 emissions and generation data by plant id and calculate co2 ratio (elec/total)

    inputs:
        eia (df): data from EIA bulk download, including calculated co2
            emissions (all total/fossil, elec total/fossil)

    outputs:
        grouped_df (df): grouped df with co2 emissions, generation, and a ratio
            of co2 from electric fossil fuels to all total (fossil+bio) fuels
    """
    cols = ['all fuel fossil co2 (kg)', 'elec fuel fossil co2 (kg)',
            'all fuel total co2 (kg)', 'elec fuel total co2 (kg)',
            'generation (mwh)']
    grouped_df = eia.groupby(['year', 'month', 'plant id'])[cols].sum()
    grouped_df.reset_index(inplace=True)
    grouped_df['co2 ratio'] = (grouped_df['elec fuel fossil co2 (kg)']
                               / grouped_df['all fuel total co2 (kg)'])
    grouped_df['co2 ratio'].fillna(0, inplace=True)

    return grouped_df

def adjust_epa_emissions(epa, eia_grouped):
    """
    Merge 2 dataframes and calculate an adjusted co2 emission for each facility.
    This adjusted value accounts for CHP and biomass emissions using calculated
    co2 emissions from fuel consumption.

    inputs:
        epa (df): monthly co2 emissions from each facility
        eia_facility (df): grouped EIA facility data

    outputs:
        epa_adj (df):
    """
    eia_keep = ['month', 'year', 'all fuel total co2 (kg)',
                'co2 ratio', 'plant id']

    epa_adj = epa.merge(eia_grouped[eia_keep],
                        on=['plant id', 'year', 'month'], how='inner')

    # epa_adj.drop(['month', 'year', 'plant id'], axis=1, inplace=True)
    epa_adj['epa index'] = (epa_adj.loc[:, 'co2_mass (kg)'] /
                            epa_adj.loc[:, 'gload (mw)'])

    # Start the adjusted co2 column with unadjusted value
    epa_adj['adj co2 (kg)'] = epa_adj.loc[:, 'co2_mass (kg)']

    # If CEMS reported co2 emissions are 0 but heat inputs are >0 and
    # calculated co2 emissions are >0, change the adjusted co2 to NaN. These
    # NaN values will be replaced by the calculated value later. Do the same
    # for low index records (<300 g/kWh). If there is a valid co2 ratio,
    # multiply the adjusted co2 column by the co2 ratio.

    epa_adj.loc[~(epa_adj['co2_mass (kg)'] > 0) &
                (epa_adj['heat_input (mmbtu)'] > 0) &
                (epa_adj['all fuel total co2 (kg)'] > 0),
                'adj co2 (kg)'] = np.nan
    epa_adj.loc[(epa_adj['epa index'] < 300) &
                (epa_adj['heat_input (mmbtu)'] > 0) &
                (epa_adj['all fuel total co2 (kg)'] > 0),
                'adj co2 (kg)'] = np.nan

    epa_adj.loc[epa_adj['co2 ratio'].notnull(),
                'adj co2 (kg)'] *= (epa_adj.loc[epa_adj['co2 ratio'].notnull(),
                                                'co2 ratio'])

    return epa_adj

def facility_co2(epa_adj, eia_facility):
    """
    Merge the plant-level adjusted epa co2 emissions with generation. Create a
    new column of final co2 emissions for each plant. Use calculated values from
    eia fuel use where epa data don't exist.

    inputs:
        epa_adj (df): Reported EPA co2 emissions for each facility by month,
            with a column for adjusted emissions
        eia_facility (df): Fuel consumption, emissions, and generation by
            facility

    outputs:
        df: merged dataframe with a "final co2 (kg)" column
    """
    merge_on = ['plant id', 'year', 'month']
    df = eia_facility.merge(epa_adj, on=merge_on, how='left')

    # keep the adjusted co2 column, but make a copy for final co2
    df['final co2 (kg)'] = df.loc[:, 'adj co2 (kg)']

    # Use calculated elec fossil co2 where adjusted epa values don't exist
    df.loc[df['final co2 (kg)'].isnull(),
           'final co2 (kg)'] = df.loc[df['final co2 (kg)'].isnull(),
                                      'elec fuel fossil co2 (kg)']

    return df

def group_fuel_cats(df, fuel_cats, fuel_col='fuel', new_col='type'):
    """
    Group fuels according to the fuel_cats dictionary inplace
    """
    for key, values in fuel_cats.items():
        df.loc[df[fuel_col].isin(values), new_col] = key

    df_grouped = df.groupby(['plant id', new_col, 'year', 'month']).sum()
    df_grouped.reset_index(inplace=True)

    keep_cols = ['plant id', new_col, 'year', 'month', 'total fuel (mmbtu)',
                 'generation (mwh)', 'elec fuel (mmbtu)',
                 'all fuel fossil co2 (kg)', 'elec fuel fossil co2 (kg)',
                 'all fuel total co2 (kg)', 'elec fuel total co2 (kg)']

    return df_grouped[keep_cols]

def state_emissions_gen(facility_gen_fuels, eia_total, ef):
    """
    Augment facility data with EIA estimates of non-reporting facilities. This
    information is only available at the state level.

    inputs:
        facility_gen_fuels: (dataframe) generation, and fuel consumption at
            facilities.
        eia_total: (dataframe) total generation and fuel consumption from all
            facilities (including non-reporting), by state
        ef: (dataframe) emission factors for fuel consumption

    output:
        state_gen_fuels: generation and fuel consumption from non-reporting
            facilities
        state_co2: co2 emissions from non-reporting facilities
    """
    # make sure both dataframes have a 'type' column and the fuel types are the
    # same.
    assert 'type' in facility_gen_fuels.columns
    assert 'type' in eia_total.columns
    facility_fuel_cats = facility_gen_fuels['type'].unique()
    total_fuel_cats = eia_total['type'].unique()
    assert set(total_fuel_cats) == set(facility_fuel_cats)



    return index_gen

def co2_calc(fuel, ef):
    """
    Calculate co2 emissions based on fuel consumption using emission factors
    from EIA/EPA and IPCC

    inputs:
        fuel: (dataframe) should have columns with the fuel type, total
            consumption, and consumption for electricity generation
        ef: (dataframe) emission factors (kg co2/mmbtu) for each fuel

    output:
        dataframe: co2 emissions from fuel consumption (total and for
        electricity) at each facility
    """


    return co2

# def add_nerc_data()