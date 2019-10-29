# coding: utf-8

# from __future__ import division
import pandas as pd
import os
from os.path import join, abspath, normpath, dirname, split
import numpy as np
from src.util import rename_cols
import json

def add_datetime(df, year='year', month='month'):
    if type(df.index) is not pd.MultiIndex:
        df['datetime'] = pd.to_datetime(df[year].astype(str) + '-' +
                                        df[month].astype(str),
                                        format='%Y-%m')
    elif 'year' in df.index.names and 'month' in df.index.names:
        year = df.index.get_level_values('year').astype(str)
        month = df.index.get_level_values('month').astype(str)
        df['datetime'] = pd.to_datetime(year + '-' + month, format='%Y-%m')

    else:
        raise IndexError('MultiIndex without year and month levels')

def add_quarter(df, year='year', month='month'):
    if 'datetime' not in df.columns:
        add_datetime(df, year, month)
    df['quarter'] = df['datetime'].dt.quarter


def facility_emission_gen(eia_facility, epa, state_fuel_cat,
                          custom_fuel_cat, export_state_cats=False,
                          print_status=True):
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
    if print_status:
        print('Renaming columns')
    rename_cols(eia_facility)
    rename_cols(epa)
    if print_status:
        print('Grouping facilities')
    eia_grouped = group_facility_data(eia_facility)

    if print_status:
        print('Adjusting EPA emissions')
    epa_adj = adjust_epa_emissions(epa, eia_grouped)

    # I need to return co2 (by facility, or just per month?) and gen/fuels by
    # fuel type.

    if print_status:
        print('Caculating CO2')
    co2 = facility_co2(epa_adj, eia_grouped)
    co2 = co2.loc[:, ['year', 'month', 'plant id', 'final co2 (kg)']]

    if print_status:
        print('Gen/fuels to state categories')
    gen_fuels_state = group_fuel_cats(eia_facility, state_fuel_cat)
    if export_state_cats:
        return co2, gen_fuels_state
    else:
        if print_status:
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
    epa_adj['epa index'] = (epa_adj.loc[:, 'co2mass_kg'] /
                            epa_adj.loc[:, 'gload_mwh'])

    # Start the adjusted co2 column with unadjusted value
    epa_adj['adj co2 (kg)'] = epa_adj.loc[:, 'co2mass_kg']

    # If CEMS reported co2 emissions are 0 but heat inputs are >0 and
    # calculated co2 emissions are >0, change the adjusted co2 to NaN. These
    # NaN values will be replaced by the calculated value later. Do the same
    # for low index records (<300 g/kWh). If there is a valid co2 ratio,
    # multiply the adjusted co2 column by the co2 ratio.

    epa_adj.loc[~(epa_adj['co2mass_kg'] > 0) &
                (epa_adj['heatinput_mmbtu'] > 0) &
                (epa_adj['all fuel total co2 (kg)'] > 0),
                'adj co2 (kg)'] = np.nan
    epa_adj.loc[(epa_adj['epa index'] < 300) &
                (epa_adj['heatinput_mmbtu'] > 0) &
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


def group_fuel_cats(df, fuel_cats, fuel_col='fuel', new_col='type',
                    extra_group_cols=[]):
    """
    Group fuels according to the fuel_cats dictionary inplace
    """
    for key, values in fuel_cats.items():
        df.loc[df[fuel_col].isin(values), new_col] = key

    group_cols = [new_col, 'year', 'month'] + extra_group_cols
    keep_cols = [new_col, 'year', 'month', 'total fuel (mmbtu)',
                 'generation (mwh)', 'elec fuel (mmbtu)',
                 'all fuel fossil co2 (kg)', 'elec fuel fossil co2 (kg)',
                 'all fuel total co2 (kg)', 'elec fuel total co2 (kg)']
    # add plant id back in if it was in the original df
    if 'plant id' in df.columns:
        group_cols += ['plant id']
        keep_cols += ['plant id']

    df_grouped = df.groupby(group_cols).sum()
    df_grouped.reset_index(inplace=True)

    return df_grouped


def extra_emissions_gen(facility_gen_fuels, eia_total, ef):
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
    # rename columns in dataframe (all lowercase)
    rename_cols(eia_total)

    # make sure both dataframes have a 'type' column and the fuel types in
    # facilities are the same as those in the eia total data.
    assert 'type' in facility_gen_fuels.columns
    assert 'type' in eia_total.columns
    facility_fuel_cats = facility_gen_fuels['type'].unique()
    total_fuel_cats = eia_total['type'].unique()
    # for fuel in facility_fuel_cats:
    #     assert fuel in total_fuel_cats

    # Only keep unique fuel codes - e.g. total solar includes SUN and DPV
    keep_types = [u'WWW', u'WND', u'WAS', u'SUN', 'DPV', u'NUC', u'NG',
       u'PEL', u'PC', u'OTH', u'COW', u'OOG', u'HPS', u'HYC', u'GEO']
    keep_cols = ['generation (mwh)', 'total fuel (mmbtu)', 'elec fuel (mmbtu)',
                 'all fuel co2 (kg)', 'elec fuel co2 (kg)']
    eia_total_monthly = (eia_total.loc[(eia_total['type'].isin(keep_types))]
                         .groupby(['type', 'year', 'month'])[keep_cols]
                         .sum())

    # Set up a MultiIndex. Useful when subtracting facility from total data
    years = facility_gen_fuels.year.unique()
    months = facility_gen_fuels.month.unique()
    iterables = [total_fuel_cats, years, months]
    index = pd.MultiIndex.from_product(iterables=iterables,
                                   names=['type', 'year', 'month'])

    # eia_extra will be the difference between total and facility
    eia_extra = pd.DataFrame(index=index, columns=['total fuel (mmbtu)',
                                                   'generation (mwh)',
                                                   'elec fuel (mmbtu)'])
    # give gen_fuels a MultiIndex
    gen_fuels = facility_gen_fuels.groupby(['type', 'year', 'month']).sum()

    # will need the IndexSlice to reference into the MultiIndex
    idx = pd.IndexSlice

    # Need to use .subtract() here because of NaN values
    use_columns=['total fuel (mmbtu)', 'generation (mwh)', 'elec fuel (mmbtu)']
    eia_extra = (eia_total_monthly.loc[:, use_columns]
                 .subtract(gen_fuels.loc[:, use_columns], fill_value=0))

    # I have lumped hydro pumped storage in with conventional hydro in the
    # facility data. Because of this, I need to add HPS rows so that the totals
    # will add up correctly. Also need to add DPV because it won't show up
    # otherwise (not in both dataframes)
    eia_extra.loc[idx[['HPS', 'DPV'],:,:],
                  use_columns] = (eia_total_monthly
                                  .loc[idx[['HPS', 'DPV'],:,:], use_columns])

    # consolidate emission factors to match the state-level fuel codes
    fuel_factors = reduce_emission_factors(ef)

    # Calculate co2 emissions for the state-level fuel categories
    eia_extra['all fuel co2 (kg)'] = 0
    eia_extra['elec fuel co2 (kg)'] = 0

    fuels = [fuel for fuel in total_fuel_cats
             if fuel in fuel_factors.keys()]
    for fuel in fuels:
        eia_extra.loc[idx[fuel,:,:],'all fuel co2 (kg)'] = \
            eia_extra.loc[idx[fuel,:,:],'total fuel (mmbtu)'] * fuel_factors[fuel]

        eia_extra.loc[idx[fuel,:,:],'elec fuel co2 (kg)'] = \
            eia_extra.loc[idx[fuel,:,:],'elec fuel (mmbtu)'] * fuel_factors[fuel]

    extra_co2 = (eia_extra.groupby(level=['type', 'year', 'month'])
                 ['all fuel co2 (kg)', 'elec fuel co2 (kg)']
                 .sum())

    extra_gen_fuel = (eia_extra
                      .drop(['all fuel co2 (kg)', 'elec fuel co2 (kg)'],
                            axis=1))

    return extra_co2, extra_gen_fuel


def reduce_emission_factors(ef, custom_reduce=None):
    """
    Reduce the standard fuel emission factors

    inputs:
        ef (df): emission factors (kg/mmbtu) for every possible fuel
        custom_reduce (dict): a custom dictionary to combine fuels. If None,
            use the default
    """
    # make sure the fuel codes are in the index
    assert 'NG' in ef.index

    if not custom_reduce:
        fuel_factors = {'NG' : ef.loc['NG', 'Fossil Factor'],
                   'PEL': ef.loc[['DFO', 'RFO'], 'Fossil Factor'].mean(),
                   'PC' : ef.loc['PC', 'Fossil Factor'],
                   'COW' : ef.loc[['BIT', 'SUB'], 'Fossil Factor'].mean(),
                   'OOG' : ef.loc['OG', 'Fossil Factor']}
    else:
        fuel_factors = custom_reduce

    return fuel_factors

def total_gen(df_list, fuel_col='fuel category'):
    pass

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


def g2lb(df):
    """
    Convert g/kWh to lb/MWh and add a column to the df
    """
    g2lb = 2.2046
    df['index (lb/mwh)'] = df['index (g/kwh)'] * g2lb


def change_since_2005(df):
    """
    Calculate the % difference from 2005 and add as a column in the df
    """
    # first calculate the index in 2005
    index_2005 = ((df.loc[df['year']==2005,'index (g/kwh)'] *
                df.loc[df['year']==2005,'generation (mwh)']) /
             df.loc[df['year']==2005,'generation (mwh)'].sum()).sum()

    # Calculated index value in 2005 is 599.8484560355034
    # If the value above is different throw an error
    # if (index_2005 > 601) or (index_2005 < 599.5):
    #     raise ValueError('Calculated 2005 index value', index_2005,
    #                      'is outside expected range. Expected value is 599.848')
    # if type(index_2005) != float:
    #     raise TypeError('index_2005 is', type(index_2005), 'rather than a float.')

    df['change since 2005'] = (df['index (g/kwh)'] - index_2005) / index_2005


def generation_index(gen_df, index_df, group_by='year'):
    """
    Calculate the emissions intensity of each fuel in each time period. Use the
    adjusted total emissions from the index dataframe to ensure that the weighted
    sum of fuel emission intensities will equal the total index value.
    """
    final_adj_co2 = index_df.loc[:,'final co2 (kg)'].copy()

    calc_total_co2 = (gen_df.groupby(group_by)['elec fuel fossil co2 (kg)']
                            .sum()
                            .values)
#     calc_total_co2 = calc_total_co2.reset_index()


    for fuel in gen_df['fuel category'].unique():
        gen_df.loc[gen_df['fuel category']==fuel, 'adjusted co2 (kg)'] = \
        (gen_df.loc[gen_df['fuel category']==fuel, 'elec fuel fossil co2 (kg)']
         / calc_total_co2 * final_adj_co2.values)

    gen_df['adjusted index (g/kwh)'] = (gen_df['adjusted co2 (kg)']
                                        / gen_df['generation (mwh)'])
    gen_df['adjusted index (lb/mwh)'] = (gen_df['adjusted index (g/kwh)']
                                         * 2.2046)
