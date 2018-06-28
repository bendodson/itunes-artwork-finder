iTunes Artwork Finder
=====================

This is the JavaScript and PHP code that powers the iTunes Artwork Finder available at [https://bendodson.com/projects/itunes-artwork-finder/](https://bendodson.com/projects/itunes-artwork-finder/)

To use on your own site, simply upload both the JavaScript and PHP files and then initialise the script with something like:

	<div>
		<form action="" method="get" accept-charset="utf-8" id="iTunesSearch">
			<select name="entity" id="entity">
				<option value="tvSeason">TV Show</option>
				<option value="movie">Movie</option>
				<option value="ebook">iBook</option>
				<option value="album">Album</option>
				<option value="software">App (iPhone or Universal)</option>
				<option value="iPadSoftware">App (iPad)</option>
				<option value="macSoftware">App (macOS)</option>
				<option value="audiobook">Audiobook</option>
				<option value="podcast">Podcast</option>
				<option value="musicVideo">Music Video (may not work)</option>
				<option value="id">Apple ID (Movie)</option>
				<option value="idAlbum">Apple ID (Album)</option>
				<option value="shortFilm">Short Film</option>
			</select>
			<input type="text" class="text" name="query" id="query" />
			<select name="country" id="country">
				<option value='us'>United States of America</option>
				<option value='gb'>United Kingdom</option>
			</select>
			<input type="submit" class="submit" value="Get the artwork" />
		</form>
	</div>

	<div id="results">

	</div>

	<script src="https://ajax.googleapis.com/ajax/libs/jquery/1.11.3/jquery.min.js"></script>
	<script src="itunes.js"></script>

You will need to amend the first line within `itunes.js` so that the `pathToAPI` variable points to the absolute URL of the `api.php` file running on a PHP server.
