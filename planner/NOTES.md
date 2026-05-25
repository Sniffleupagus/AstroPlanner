### horizon_scan.py

- binary_search_boundary
  - do not "first check the highest"  that move up and back takes too long
    and usually i set up such that overhead is clear, so this just takes
    a lot of time
  - Instead of checking the bounds, because the lower pretty much will be obstructed (and the scopes wont goto anything below 30 or 35.. i forget which), start with where you found the horizon previously:
     - start at previous horizon, or (hi+low)/2 for first one
     - if sky then go down until obstructed
     - else if obstruction then go up until sky
     - compute horizon
     - move to next one

but it does seem to work. it just takes a long time. I think starting where you expect the horizon, then move up or down to find it will be more efficient. it does not need an exhaustive scan (though if it is doing exhaustive, saving the images to stitch together into a 3D panorama would be cool)


# I edited some of the timeout to be longer, because the logs were showing lots of checks between results. I figure longer timeouts will not error (i dont recall what you said messed up on it with rapid messages)  the seestar if we start adding other things.

## can the plot library that does the web page do a

## skymap.html
- can we get names for the targets in the tooltip that pops up? Like M42 - Orion Nebula
- horizon line does something weird. i have a photo. it is hard to describe.

- I would like another view where, once zoomed in enough it shows the stacked FITS file (or a representative file), scaled to its size in the sky (so mosaics would be bigger than individual shots). Probably not loading the full sized image.  if there is a jpeg or png, or a thumbnail image, that would probably work.  fits are too much.
   - so when zoomed in, all the imaged would be scaled and rotated as they are in space, doing a really bad full-sky stack, sort of.  But only when zoomed in, because it probably would not work with every single session respresented (or would it?) but i would like to be able to see what is there, not just the heat map.
  - controls to advance time forward and back to see where the view and the sun will be.  

- i am not quite understanding the chart and how it maps to my view.
  - North is top, west is to the left, east to the right?
  - and the top edge of the image is really a point (the top of the RaDec sphere), stretched across. left and right edges wrap around.
  
  - the sun is cool. Can you add the moon?
  - does it update periodically? i thought i saw it move, but maybe it reloaded. it does.  yay!
  
