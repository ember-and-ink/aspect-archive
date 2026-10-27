"""The card archive as a readable collection: the in-memory index over the cards
on disk (`catalog`) and the thumbnail cache that fronts them (`thumbs`).

`catalog`, not `index` -- the module defines a function named `index()`, and a
module of the same name inside this package would make `from aspect.archive import
index` ambiguous between the two.
"""
