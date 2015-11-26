iTunes Artwork Finder
=====================

This is the JavaScript and PHP code that powers the iTunes Artwork Finder available at [http://bendodson.com/projects/itunes-artwork-finder/](http://bendodson.com/projects/itunes-artwork-finder/)

To use on your own site, simply upload both the JavaScript and PHP files and then initialise the script with something like:

	<article>
		<header>
			<h3>iTunes Artwork Finder</h3>
			<form action="" method="get" accept-charset="utf-8" id="iTunesSearch">
				<select name="entity" id="entity">
					<option value="tvSeason">TV Show</option>
					<option value="movie">Movie</option>
					<option value="ebook">iBook</option>
					<option value="album">Album</option>
					<option value="software">App</option>
					<option value="audiobook">Audiobook</option>
					<option value="podcast">Podcast</option>
					<option value="musicVideo">Music Video (may not work)</option>
					<option value="id">Apple ID (Movies)</option>
					<option value="shortFilm">Short Film</option>
				</select>
				<input type="text" class="text" name="query" id="query" />
				<select name="country" id="country">
					<option value='us'>United States of America</option>
					<option value='gb'>United Kingdom</option>
					<option value=''>---</option>
				</select>
				<input type="submit" class="submit" value="Get the artwork" />
			</form>
		</header>

		<div id="results">

		</div>
	</article>

	<script src="https://ajax.googleapis.com/ajax/libs/jquery/1.11.3/jquery.min.js"></script>
	<script src="itunes.js"></script>
