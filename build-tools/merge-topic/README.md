# gerrit-topic-picker

## Features for repositories managed by repo tool

- Can filter status

      merge-topic.py --status/-s open --status/-s merged .. --status/-s whatever

- Can avoid re-downloading a review if a commit with the Change-Id is present

      merge-topic.py --avoid-re-download/-ard

- Specify one or more topics

      merge-topic.py --topic target-topic-1 --topic target-topic-2
      merge-topic.py -t target-topic

- gerrit URL is automatically discovered from the .gitreview file for each repo

- Specify download strategy

      merge-topic.py --download-strategy/-ds "Cherry Pick"
      merge-topic.py --download-strategy/-ds "Pull"

- Specify script to be run in case of merge conflict. A simple fixer is supplied by "pick_both_merge_fixer.py".  
  Otherwise provide your own.

      merge-topic.py --merge-fixer pick_both_merge_fixer.py
      merge-topic.py --merge-fixer my_script.sh
      merge-topic.py --merge-fixer my_script.py
      merge-topic.py --merge-fixer my_script.runnable

## Usage for repositories managed by repo tool:

Set the `MY_REPO_ROOT_DIR` environment variable to the repo root directory (the one which contains the `.repo` dir)

    MY_REPO_ROOT_DIR=/path/to/repo/ python3 merge-topic.py repo --help

Example usage in real life:

    MY_REPO_ROOT_DIR=/here \
        python3 merge-topic.py repo \
        --topic my-topic \
        --download-strategy "Cherry Pick" \
        --status open \
        --avoid-re-download
    # OR short
    MY_REPO_ROOT_DIR=/here \
        python3 merge-topic.py repo \
        -t my-topic \
        -ds "Cherry Pick" \
        -s open \
        -ard


    # fails a cherry-pick: CalledProcessError(1, ' git cherry-pick FETCH_HEAD')

    # resolve the cherry-pick merge errors
    # then invoke the tool againg with the same parameters
    # repeat the process until all commits are synced

Example usage for specifying a script that could automatically fix merge conflicts:

    MY_REPO_ROOT_DIR=/here \
        python3 merge-topic.py repo \
        --topic my-topic \
        --download-strategy "Cherry Pick" \
        --status open \
        --avoid-re-download \
        --merge-fixer dummy_merge_fixer.py

Example real life usage with merge fixer that picks changes from both sources:

    MY_REPO_ROOT_DIR=/here \
        python3 merge-topic.py repo \
        --topic my-topic \
        --download-strategy "Cherry Pick" \
        --status open \
        --avoid-re-download \
        --merge-fixer pick_both_merge_fixer.py

Example usage for syncing open and merged reviews:

    MY_REPO_ROOT_DIR=/here \
        python3 merge-topic.py repo \
        --topic my-topic \
        --download-strategy "Cherry Pick" \
        --status open \
        --status merged \
        --avoid-re-download

## Future Work

- Pick relation chain
- Improve merge fixer logging
- Fully automate merge fixer
