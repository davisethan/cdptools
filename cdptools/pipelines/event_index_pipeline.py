#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Union

import dask.config
import dask.dataframe as dd
import pandas as pd
from dask_cloudprovider import FargateCluster
from distributed import Client, LocalCluster
from nltk import ngrams
from nltk.stem import PorterStemmer
from prefect import Flow, task, unmapped
from prefect.engine.executors import DaskExecutor

from ..databases import Database, OrderOperators
from ..dev_utils import load_custom_object
from ..file_stores import FileStore
from ..indexers import Indexer
from .pipeline import Pipeline

###############################################################################

log = logging.getLogger(__name__)

###############################################################################
# Workflow tasks


@task
def get_events(db: Database) -> pd.DataFrame:
    # Get transcript dataset
    return pd.DataFrame(db.select_rows_as_list("event", limit=int(1e6)))


@task
def get_event_ids(events: pd.DataFrame) -> List[str]:
    return list(events.event_id)


@task
def get_bodies(db: Database) -> List[Dict]:
    return db.select_rows_as_list("body")


@task
def get_transcript_details(event_id: str, db: Database) -> Dict:
    # Get the highest confidence transcript for the event
    results = db.select_rows_as_list(
        table="transcript",
        filters=[("event_id", event_id)],
        order_by=("confidence", OrderOperators.desc),
        limit=1,
    )

    # Return result if found
    if len(results) == 1:
        return results[0]


@task
def construct_transcript_df(transcripts: List[Union[Dict, None]]) -> pd.DataFrame:
    # Create transcripts dataframe
    return pd.DataFrame(
        [
            transcript_details
            for transcript_details in transcripts
            if transcript_details is not None
        ]
    )


@task
def get_file_ids(transcripts: pd.DataFrame) -> List[str]:
    return list(transcripts.file_id)


@task
def get_file_details(file_id: str, db: Database) -> Dict:
    return db.select_row_by_id("file", file_id)


@task
def merge_event_and_body_details(
    events: pd.DataFrame, bodies: List[Dict]
) -> pd.DataFrame:
    # Create bodies dataframe
    bodies = pd.DataFrame(bodies)

    return events.merge(bodies, on="body_id", suffixes=("_event", "_body"),)


@task
def merge_transcript_and_file_details(
    transcripts: pd.DataFrame, files: List[Dict]
) -> pd.DataFrame:
    # Create files dataframe
    files = pd.DataFrame(files)

    return transcripts.merge(files, on="file_id", suffixes=("_transcript", "_file"),)


@task
def merge_event_and_transcript_details(
    events: pd.DataFrame, transcripts: pd.DataFrame,
) -> pd.DataFrame:
    # Merge dataframes
    merged = transcripts.merge(
        events, on="event_id", suffixes=("_event", "_transcript")
    )
    merged.to_csv("transcript_manifest.csv", index=False)

    return merged


@task
def corpus_to_dict(corpus: pd.DataFrame) -> List[Dict]:
    return corpus[["event_id", "uri"]].to_dict("records")


@task
def read_transcript_and_generate_grams(
    document_details: Dict,
    n_gram_size: int,
    fs: FileStore,
) -> List[Dict]:
    # Create corpus dir if not already existing
    corpus_dir = Path("corpus")
    corpus_dir.mkdir(exist_ok=True)

    # Get remote file name for local storage
    filename = document_details["uri"].split("/")[-1]
    doc_path = corpus_dir / filename

    # Download transcript and read
    if not doc_path.exists():
        doc_path = fs.download_file(filename, doc_path)

    # Get raw text
    raw_doc = Indexer.get_raw_transcript(doc_path)

    # Get list of cleaned sentences
    sentences = [s for s in raw_doc.split(". ") if len(s) > 0]
    cleaned_sentences = [Indexer.clean_doc(s) for s in sentences]
    cleaned_sentences = [s for s in cleaned_sentences if s is not None]

    # Get ngrams
    grams = []
    for cleaned_sentence in cleaned_sentences:
        grams += [*ngrams(cleaned_sentence.split(), n_gram_size)]

    # Spawn stemmer
    ps = PorterStemmer()

    # Create list of grams
    ngram_results = []
    for gram in grams:
        # Join ngram to single string
        unstemmed_gram = " ".join(gram)

        # Get context span
        for cleaned_sentence in cleaned_sentences:
            if unstemmed_gram in cleaned_sentence:
                context_span = cleaned_sentence

        # Join, lower, and stem the gram
        stemmed_gram = " ".join([ps.stem(term.lower()) for term in gram])

        ngram_results.append({
            "event_id": document_details["event_id"],
            "unstemmed_gram": unstemmed_gram,
            "stemmed_gram": stemmed_gram,
            "context_span": context_span,
        })

    return ngram_results


@task
def flatten(items: Iterable[Iterable]) -> List:
    return [item for iterable in items for item in iterable]


@task
def reduce_grams_to_term_frequencies(grams: dd.DataFrame) -> dd.DataFrame:
    pass


@task
def store_dd_df(rows: List[Dict], filename: str) -> dd.DataFrame:
    df = dd.from_pandas(pd.DataFrame(rows), chunksize=10000)
    df.to_csv(filename, index=False)

    return df


###############################################################################


class EventIndexPipeline(Pipeline):
    def __init__(self, config_path: Union[str, Path]):
        # Resolve config path
        config_path = config_path.resolve(strict=True)

        # Read
        with open(config_path, "r") as read_in:
            self.config = json.load(read_in)

        # Get workers
        self.n_workers = self.config.get("max_synchronous_jobs")

        # Load modules
        self.database = load_custom_object.load_custom_object(
            module_path=self.config["database"]["module_path"],
            object_name=self.config["database"]["object_name"],
            object_kwargs={**self.config["database"].get("object_kwargs", {})},
        )
        self.file_store = load_custom_object.load_custom_object(
            module_path=self.config["file_store"]["module_path"],
            object_name=self.config["file_store"]["object_name"],
            object_kwargs=self.config["file_store"].get("object_kwargs", {}),
        )
        self.indexer = load_custom_object.load_custom_object(
            module_path=self.config["indexer"]["module_path"],
            object_name=self.config["indexer"]["object_name"],
            object_kwargs=self.config["indexer"].get("object_kwargs", {}),
        )

    def run(self):
        # Construct workflow
        with Flow("Event Index Pipeline") as flow:
            # Get events and body information
            events = get_events(self.database)
            event_ids = get_event_ids(events)
            bodies = get_bodies(self.database)

            # Get each event's transcript information
            transcripts = get_transcript_details.map(
                event_ids, db=unmapped(self.database),
            )
            transcripts = construct_transcript_df(transcripts)
            file_ids = get_file_ids(transcripts)
            files = get_file_details.map(file_ids, db=unmapped(self.database))

            # Merge dataframes
            events = merge_event_and_body_details(events, bodies)
            transcripts = merge_transcript_and_file_details(transcripts, files)
            transcripts = merge_event_and_transcript_details(events, transcripts)

            # Construct delayed text get
            corpus = corpus_to_dict(transcripts)

            # Get uni, bi, and tri grams
            unigrams = read_transcript_and_generate_grams.map(
                corpus, n_gram_size=unmapped(1), fs=unmapped(self.file_store)
            )
            bigrams = read_transcript_and_generate_grams.map(
                corpus, n_gram_size=unmapped(2), fs=unmapped(self.file_store)
            )
            trigrams = read_transcript_and_generate_grams.map(
                corpus, n_gram_size=unmapped(3), fs=unmapped(self.file_store)
            )

            # Flatten uni, bi, and tri grams
            unigrams = flatten(unigrams)
            bigrams = flatten(bigrams)
            trigrams = flatten(trigrams)

            # Store ngram results
            store_dd_df(unigrams, "unigrams-*.csv")
            store_dd_df(bigrams, "bigrams-*.csv")
            store_dd_df(trigrams, "trigrams-*.csv")

        # Configure dask config
        dask.config.set(
            {"scheduler.work-stealing": False}
        )

        # Construct Dask Cluster
        # cluster = LocalCluster()
        cluster = FargateCluster(
            "councildataproject/cdptools-beta",
            worker_cpu=1024,
            worker_mem=8192,
        )
        cluster.adapt(minimum=10, maximum=100)
        client = Client(cluster)

        log.info(f"Dashboard available at: {client.dashboard_link}")

        # Run
        state = flow.run(executor=DaskExecutor(address=cluster.scheduler_address))

        # Visualize
        flow.visualize(filename="event-index-pipeline", format="png")
