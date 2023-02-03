import sys
from datetime import timedelta
from time import perf_counter
import pandas as pd
import logging
from os import path
import traceback
import warnings
import yaml
from pybedtools import BedTool

from helper import ConfigurationFile, init_logger, update_cas_offinder_path, update_flashfry_path
from configuration_files.const import DB_NAME_LIST, CONF_FILE, CAS_OFFINDER_OUTPUT_PATH, FLASHFRY_OUTPUT_PATH, \
    YAML_CONFIG_FILE
from db import GencodeDb, OmimDb, MirGeneDB, HumanTFDb, ProteinAtlas, RBP, COSMIC, ReMapEPD, EnhancerAtlas, Pfam, \
    initialize_off_target_df, analyze_with_id_list, add_db, calculate_score, save_global_off_target_results, \
    save_db_result, update_database_base_path, get_database_path, TargetScan
from off_target import run_flashfry, run_cas_offinder_api, run_cas_offinder_locally, load_off_target_from_file, \
    load_cas_offinder_off_target, load_flashfry_off_target
from obj_def import OffTarget

warnings.filterwarnings("ignore", category=RuntimeWarning)
pd.options.mode.chained_assignment = None

log = logging.getLogger("Base_log")


def extract_data(db_name_list, input_file=None):
    """
    Run intersection between the off-target file to other databases specified in db_name_list

    :return:
    """
    log.debug("Starting to extract data")

    off_target_df = None
    flashfry_score = pd.DataFrame()
    time_start = perf_counter()

    if input_file:
        log.info("Loading off-target from a file in: {}".format(input_file))
        if path.exists(input_file):
            off_target_df = load_off_target_from_file(input_file)
        else:
            raise Exception("There is no file in the path: {}. Please verify the file was created."
                            .format(CAS_OFFINDER_OUTPUT_PATH))
    else:
        log.info("Loading local off-target")
        off_target_co_df, off_target_ff_df = None, None
        if path.exists(CAS_OFFINDER_OUTPUT_PATH):
            off_target_co_df = load_cas_offinder_off_target()
        if path.exists(FLASHFRY_OUTPUT_PATH):
            off_target_ff_df, flashfry_score = load_flashfry_off_target()

        # If both files was loaded, merge them
        if (off_target_co_df is not None) & (off_target_ff_df is not None):
            off_target_df = pd.concat([off_target_co_df, off_target_ff_df])
            off_target_df = off_target_df.astype({OffTarget.get_field_title("chromosome"): "category",
                                                  OffTarget.get_field_title("start"): int,
                                                  OffTarget.get_field_title("end"): int,
                                                  OffTarget.get_field_title("strand"): "category"})
            off_target_df.drop_duplicates(subset=[OffTarget.get_field_title("chromosome"),
                                                  OffTarget.get_field_title("start"),
                                                  OffTarget.get_field_title("end"),
                                                  OffTarget.get_field_title("strand")], inplace=True)

        elif off_target_co_df is not None:
            off_target_df = off_target_co_df
        elif off_target_ff_df is not None:
            off_target_df = off_target_ff_df
        if off_target_df is not None:
            off_target_df[OffTarget.get_field_title("chromosome")] = \
                off_target_df[OffTarget.get_field_title("chromosome")].apply(lambda s: s.strip("chr"))
            if OffTarget.get_field_title("id") not in off_target_df.columns:
                off_target_df[OffTarget.get_field_title("id")] = "."
            if OffTarget.get_field_title("sequence") not in off_target_df.columns:
                off_target_df[OffTarget.get_field_title("sequence")] = "."

    # Create the bed file
    if off_target_df is not None:
        off_target_df.reset_index(drop=True, inplace=True)
        off_target_df["name"] = off_target_df.index
        off_target_bed = BedTool.from_dataframe(off_target_df, na_rep=".").sort()
    else:
        raise Exception("Off target dataframe is empty. Please verify there is files in the output folder")

    # Initialize the result
    db_list = []
    off_target_df = initialize_off_target_df(off_target_df)
    time_end = perf_counter()
    log.info("Total run for off-target initialization: {}".format(timedelta(seconds=(time_end - time_start))))

    conf_yaml = None
    with open(YAML_CONFIG_FILE) as f:
        conf_yaml = yaml.load(f, Loader=yaml.FullLoader)

    # Start extracting information from the databases
    log.info("Begin to run intersection between off-target and data")
    gencode_dependent = ["omim", "humantf", "rbp", "protein_atlas", "cosmic"]
    remap_epd_dependent = ["omim", "cosmic"]
    enhancer_atlas_dependent = ["omim", "cosmic"]
    gencode_db = None
    remap_epd_db = None
    enhancer_atlas_db = None
    for current_db_name in db_name_list:
        time_start = perf_counter()
        current_db = None
        file_path = conf_yaml["databases"][current_db_name]["human"].get("path", "")
        final_columns = conf_yaml["databases"][current_db_name]["human"].get("columns", [])
        if current_db_name == "gencode":
            current_db = GencodeDb(file_path, final_columns)
            gencode_db = current_db
        elif current_db_name == "mirgene":
            current_db = MirGeneDB(file_path, final_columns)
        elif current_db_name == "remapepd":
            current_db = ReMapEPD(file_path, final_columns)
            remap_epd_db = current_db
        elif current_db_name == "enhanceratlas":
            current_db = EnhancerAtlas(file_path, final_columns)
            enhancer_atlas_db = current_db
        elif current_db_name == "pfam":
            current_db = Pfam(file_path, final_columns)
        elif current_db_name == "targetscan":
            current_db = TargetScan(file_path, final_columns)
        elif current_db_name == "omim":
            current_db = OmimDb(file_path, final_columns)
        elif current_db_name == "humantf":
            current_db = HumanTFDb(file_path, final_columns)
        elif current_db_name == "protein_atlas":
            current_db = ProteinAtlas(file_path)
        elif current_db_name == "rbp":
            current_db = RBP(file_path, final_columns)
        elif current_db_name == "cosmic":
            current_db = COSMIC(file_path, final_columns)

        if current_db:
            # Analyze  - intersect between GENCDOE result to the the DB columns for intersection.
            if (current_db_name in gencode_dependent) and (gencode_db is not None) and \
                    (gencode_db.complete_result.get("gene_ensembl_id", None) is not None):
                analyze_with_id_list(current_db, off_target_df,
                                     gencode_db.complete_result["gene_ensembl_id"].unique(),
                                     "complete_result", "gene_ensembl_id")
            # Analyze  - intersect between off-target location to the the DB location with BEDTools.
            else:
                current_db.analyze(off_target_bed)

            # Analyze  - intersect between Enhancer Atlas result to the the DB columns for intersection.
            if (current_db_name in enhancer_atlas_dependent) and (enhancer_atlas_db is not None) and \
                    (enhancer_atlas_db.complete_result.get("gene_ensembl_id", None) is not None):
                analyze_with_id_list(current_db, off_target_df,
                                     enhancer_atlas_db.complete_result["gene_ensembl_id"].unique(),
                                     "enhancer_atlas", "enhancer_atlas_gene_ensembl_id")

            # Analyze  - intersect between ReMap EPD result to the the DB columns for intersection.
            if (current_db_name in remap_epd_dependent) and (remap_epd_db is not None) and \
                    (remap_epd_db.complete_result.get("gene_ensembl_id", None) is not None):
                analyze_with_id_list(current_db, off_target_df,
                                     remap_epd_db.complete_result["gene_ensembl_id"].unique(),
                                     "remap_epd", "remap_epd_gene_ensembl_id")

            off_target_df = current_db.process_result(off_target_df)
            add_db(db_list, current_db)
            time_end = perf_counter()
            log.info("Total run for {} analyze: {}".format(current_db.get_db_name(),
                                                           timedelta(seconds=(time_end - time_start))))
        else:
            log.info("No DB was created. current DB name: {}".format(current_db_name))

    log.info("Saving the results")
    time_start = perf_counter()
    off_target_df = calculate_score(off_target_df)
    ot_results = save_global_off_target_results(off_target_df, flashfry_score, conf_yaml["off_target_result_columns"])
    db_results = save_db_result(db_list)
    time_end = perf_counter()
    log.info("Total run for saving: {}".format(timedelta(seconds=(time_end - time_start))))
    log.info("Clearing the result")

    return ot_results, db_results


def main():
    # Build parser
    total_time_start = perf_counter()

    args = ConfigurationFile.parse_file(CONF_FILE).dict()

    # Initialize log
    log_level = logging.INFO
    if args.get("debug", False):
        log_level = logging.DEBUG
    init_logger(debug_level=log_level, logger_name="Base_log")

    log.info("Starting to run off-tov locally")

    try:
        # Update database base directory path
        if args.get("update_db_base_dir_path", None):
            time_start = perf_counter()
            update_database_base_path(args["update_db_base_dir_path"])
            time_end = perf_counter()
            log.info("Total run for updating databases base path: {}".format(
                timedelta(seconds=(time_end - time_start))))

        # Option to run cas-offinder
        if args.get("run_cas_offinder", None):
            if args.get("cas_offinder_path", None):
                update_cas_offinder_path(args["cas_offinder_path"])
            run_cas_offinder_locally(args["run_cas_offinder"])

        if args.get("run_cas_offinder_api", None):
            run_cas_offinder_api(args["run_cas_offinder_api"])

        # Option to run FlashFry
        if args.get("run_flashfry", None):
            input_flashfry = args["run_flashfry"]
            if args.get("flashfry_path", None):
                update_flashfry_path(args["flashfry_path"])
            run_flashfry(database_path=get_database_path(), commands=input_flashfry)

        # Option to update the databases
        # if ("update_db" in args_key) & (args["update_db"]):
        #     t.start()
        #     db.update_database()
        #     time = t.stop()
        #     log.info("Total run for updating databases: {}".format(time))

        # Option to run intersection and which databases
        if args.get("db_list", None):
            input_file = None
            if args.get("input_file_path", None):
                input_file = args["input_file_path"]
            if "all" in args.get("db_list", []):
                db_name_list = DB_NAME_LIST
            else:
                db_name_list = args.get("db_list", [])
            extract_data(db_name_list=db_name_list, input_file=input_file)

    except Exception as e:
        log.error("{}\nTrace: {}".format(e, traceback.print_exc()))
        log.info("An error has occurred. Existing.")

    total_time_end = perf_counter()
    log.info("Finish running in {}. Bye Bye :)".format(timedelta(seconds=(total_time_end - total_time_start))))


if __name__ == "__main__":
    main()