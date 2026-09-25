"""What every Stash processing worker is built on: the stage-agnostic
`Worker`, its runtimes (local consumer loop, AWS Lambda), the shared item
SQL, S3 store, OpenAI client setup and settings base. Each worker is its own
package depending on this one; workers never depend on each other."""
