## Travis CI Integration

This project is configured to be automatically tested using the Travis continuous integration system, https://travis-ci.org. The travis [getting started](https://docs.travis-ci.com/user/getting-started) page nicely documents the procedure to set this up, but to summarize that:
1. Sign into the travis-ci website and grant Travis just enough access to your repo to allow Travis to be called when the repo changes (for public repos, this is a very limited permissions bit).
2. On your travis profile page, enable travis CI for the repo you want to test.
3. Drop a .travis.yaml file at the root of the repository that tells travis how to test the project.
4. Push the change. You'll see new UI things inline with the standard github views of project activity. Click them for details.
