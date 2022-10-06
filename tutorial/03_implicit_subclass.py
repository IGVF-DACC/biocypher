from data_generator import Protein, EntrezProtein

import biocypher


def main():
    # Setup: create a list of proteins to be imported
    proteins = [
        p
        for sublist in zip(
            [Protein() for _ in range(10)],
            [EntrezProtein() for _ in range(10)],
        )
        for p in sublist
    ]

    # Extract id, label, and property dictionary
    def node_generator():
        for protein in proteins:
            yield (protein.id, protein.label, protein.properties)

    # Create BioCypher driver
    driver = biocypher.Driver(
        offline=True,  # start without connecting to Neo4j instance
        db_name="implicit_subclass",  # name of database for import call
        user_schema_config_path="tutorial/03_schema_config.yaml",
    )
    # Run the import
    driver.write_nodes(node_generator())

    # Write command line call
    driver.write_import_call()


if __name__ == "__main__":
    main()
