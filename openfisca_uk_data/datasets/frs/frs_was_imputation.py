from openfisca_uk_data.utils import dataset, UK
from openfisca_uk_data.datasets.frs.frs import FRS
import pandas as pd
import microdf as mdf
import sklearn
import synthimpute as si
import h5py
import numpy as np

@dataset
class FRS_WAS_Imputation:
	name = "frs_was_imp"
	model = UK

	def generate(was_file: str, year: int):
		was_df = process_was(was_file)
		pred_land = impute_land(was_df, year)
		frs_was = h5py.File(FRS_WAS_Imputation.file(year), mode="w")
		frs = FRS.load(year)
		for variable in tuple(frs.keys()):
			frs_was[variable] = np.array(frs[variable][...])
		frs_was["land_value"] = pred_land.values
		frs_was.close()
		frs.close()


def impute_land(was_df: pd.DataFrame, year: int) -> pd.Series:
	"""Impute land by fitting a random forest model.

	Args:
		was_df (pd.DataFrame): The WAS dataset.
		year (int): The year of simulation.

	Returns:
		pd.Series: The predicted land values.
	"""
	from openfisca_uk import Microsimulation
	sim = Microsimulation(dataset=FRS, year=year)

	was = pd.read_csv("~/was.csv")

	TRAIN_COLS = [
		"gross_income",
		"num_adults",
		"num_children",
		"pension_income",
		"employment_income",
		"self_employment_income",
		"investment_income",
		"num_bedrooms",
		"council_tax",
		"is_renting",
		# "region"  # TODO: add region mapping from WAS int.
	]

	IMPUTE_COLS = [
		"est_land",  # Total wealth.
	]

	# TODO: Test imputation quantiles on a hold out sample.
	train, test = sklearn.model_selection.train_test_split(was)
	test["pred_land"] = si.rf_impute(
		x_train=train[TRAIN_COLS],
		y_train=train[IMPUTE_COLS],
		x_new=test[TRAIN_COLS],
		sample_weight_train=train.weight,
	)

	# FRS has investment income split between dividend and savings interest.
	frs_cols = [i for i in TRAIN_COLS if i != "investment_income"]
	frs_cols += [
		"dividend_income",
		"savings_interest_income",
		"people",
		"net_income",
	]
	frs_cols

	frs = sim.df(frs_cols, map_to="household", period=year)
	frs["investment_income"] = frs.savings_interest_income + frs.dividend_income

	frs["pred_land"] = si.rf_impute(
		x_train=was[TRAIN_COLS],
		y_train=was[IMPUTE_COLS],
		x_new=frs[TRAIN_COLS],
		sample_weight_train=was.weight,
	)

	# Adjust the imputed land values to match the actual land values.
	total_initial_pred_land = frs.pred_land.sum()

	total_was_land = mdf.weighted_sum(was, "est_land", "weight")

	frs.pred_land *= total_was_land / total_initial_pred_land

	return frs.pred_land


def process_was(was_file: str) -> pd.DataFrame:
	"""Process the Wealth and Assets Survey household wealth file.

	Args:
		was_file (str): Path to the file: was_round_6_hhold_eul_mar_20.tab.

	Returns:
		pd.DataFrame: The processed dataframe.
	"""
	RENAMES = {
		"R6xshhwgt": "weight",
		# Components for estimating land holdings.
		"DVLUKValR6_sum": "uk_land",
		"DVPropertyR6": "property_values",
		"DVFESHARESR6_aggr": "emp_shares_options",
		"DVFShUKVR6_aggr": "uk_shares",
		"DVIISAVR6_aggr": "investment_isas",
		"DVFCollVR6_aggr": "unit_investment_trusts",
		"TotpenR6_aggr": "pensions",
		"DvvalDBTR6_aggr": "db_pensions",
		# Predictors for fusing to FRS.
		"dvtotgirR6": "gross_income",
		"NumAdultW6": "num_adults",
		"NumCh18W6": "num_children",
		# Household Gross Annual income from occupational or private pensions
		"DVGIPPENR6_AGGR": "pension_income",
		"DVGISER6_AGGR": "self_employment_income",
		# Household Gross annual income from investments
		"DVGIINVR6_aggr": "investment_income",
		# Household Total Annual Gross employee income
		"DVGIEMPR6_AGGR": "employment_income",
		"HBedrmW6": "num_bedrooms",
		"GORR6": "region",
		"DVPriRntW6": "is_renter",  # {1, 2} TODO: Get codebook values.
		"CTAmtW6": "council_tax",
		# Other columns for reference.
		"DVLOSValR6_sum": "non_uk_land",
		"HFINWNTR6_Sum": "net_financial_wealth",
		"DVLUKDebtR6_sum": "uk_land_debt",
		"HFINWR6_Sum": "gross_financial_wealth",
		"TotWlthR6": "wealth",
	}

	was = (
		pd.read_csv(
			was_file,
			usecols=RENAMES.keys(),
			delimiter="\t",
			low_memory=False
		)
		.replace(" ", 0)
		.astype(float)
		.rename(columns=RENAMES)
	)

	was["is_renting"] = was["is_renter"] == 1

	# Land value held by households and non-profit institutions serving
	# households: 3.9tn as of 2019 (ONS).
	HH_NP_LAND_VALUE = 3_912_632e6
	# Land value held by financial and non-financial corporations.
	CORP_LAND_VALUE = 1_600_038e6
	# Land value held by government (not used).
	GOV_LAND_VALUE = 196_730e6

	was["non_db_pensions"] = was.pensions - was.db_pensions
	was["corp_wealth"] = was[
		[
			"non_db_pensions",
			"emp_shares_options",
			"uk_shares",
			"investment_isas",
			"unit_investment_trusts",
		]
	].sum(axis=1)

	totals = mdf.weighted_sum(
		was, ["uk_land", "property_values", "corp_wealth"], "weight"
	)

	land_prop_share = (HH_NP_LAND_VALUE - totals.uk_land) / totals.property_values
	land_corp_share = CORP_LAND_VALUE / totals.corp_wealth

	was["est_land"] = (
		was.uk_land
		+ was.property_values * land_prop_share
		+ was.corp_wealth * land_corp_share
	)

	return was